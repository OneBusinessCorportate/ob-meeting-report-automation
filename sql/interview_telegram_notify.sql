-- Telegram DIGEST notification on interview outcomes (Supabase trigger + pg_net),
-- with a self-healing pg_cron retry so messages are delivered even when the
-- Supabase -> api.telegram.org egress times out intermittently.
--
-- WHAT FIRES A SEND (any recommendation — hire / maybe / reject / training):
--   1) status -> 'analysis_done' on intv_interviews  (the normal first pass), and
--   2) ANY new completed row in intv_analyses          (covers re-analysis / a
--      directly inserted analysis where the interview was ALREADY analysis_done,
--      so "something new in Supabase" always sends, not only status transitions).
-- Both just mark a pending change; one immediate attempt is made on the status
-- transition, and a 1-minute cron (intv_retry_digest) sends/resends until
-- Telegram confirms HTTP 200 for that exact change.
--
-- The digest lists EVERY processed candidate once, each with its 5-thesis
-- evaluation + a short (first-sentence) summary. It is split into as many
-- ≤4096-char messages as needed, keeping each candidate whole — nothing is ever
-- truncated or cut mid-word. On 'error': a short immediate message.
--
-- Secrets from Supabase Vault (telegram_bot_token / telegram_chat_id), ASCII only.
-- Stays silent until they exist. Disable everything:
--   select cron.unschedule('intv_retry_digest');
--   drop trigger trg_intv_notify_telegram on public.intv_interviews;
--   drop trigger trg_intv_analyses_notify on public.intv_analyses;

create extension if not exists pg_net;
create extension if not exists pg_cron;

create table if not exists public.intv_notify_state (
  id boolean primary key default true,
  last_change_at      timestamptz,
  last_attempt_at     timestamptz,
  last_request_id     bigint,
  requested_change_at timestamptz,   -- which change the last request was sent for
  delivered_change_at timestamptz,
  constraint intv_notify_state_singleton check (id)
);
insert into public.intv_notify_state(id) values (true) on conflict do nothing;
-- Older databases: add the attribution column if the table predates it.
alter table public.intv_notify_state add column if not exists requested_change_at timestamptz;

-- Helper: one Telegram sendMessage, returns the pg_net request id.
create or replace function public.intv_tg_send(p_tok text, p_chat text, p_text text)
returns bigint
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare r bigint;
begin
  select net.http_post(
    url := 'https://api.telegram.org/bot' || p_tok || '/sendMessage',
    headers := jsonb_build_object('Content-Type','application/json'),
    body := jsonb_build_object('chat_id', p_chat, 'text', p_text, 'disable_web_page_preview', true),
    timeout_milliseconds := 20000
  ) into r;
  return r;
end;
$$;

-- Shorten prose to its first COMPLETE sentence (>= ~50 chars, ending on .!?), so
-- the digest stays compact without cutting mid-word/idea. Text with no sentence
-- break is kept whole (it is already a single idea).
create or replace function public.intv_first_sentence(t text)
returns text
language sql immutable
as $$
  select case
    when t is null or btrim(t) = '' then null
    else coalesce(substring(btrim(t) from '^.{50,}?[.!?]'), btrim(t))
  end;
$$;

-- Build Telegram-safe message chunks. Each candidate's block (5-thesis breakdown
-- + the per-candidate structure, with SHORT first-sentence Кратко/Обоснование)
-- is kept WHOLE in one message. Whole candidates are packed until the next won't
-- fit, then a new message starts. Only a single candidate larger than one message
-- is line/word-split. Nothing is ever cut mid-word or dropped. No side effects.
create or replace function public.intv_digest_chunks(v_limit int default 3800)
returns text[]
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare
  v_count int; v_header text; v_chunk text := '';
  out_arr text[] := '{}'; b text; ln text; rest text; piece text; n int;
begin
  select count(*) into v_count
  from public.intv_interviews i
  join public.intv_analyses a on a.interview_id = i.id and a.is_current and a.status='completed'
  where i.status='analysis_done';

  v_header := '📋 Обучающий центр — обработанные собеседования (' || coalesce(v_count,0) || ')';

  for b in
    with base as (
      select i.id, c.full_name, i.interview_type, i.call_url,
             a.recommendation, s.overall_score, a.summary,
             a.candidate_strengths, a.candidate_weaknesses, a.next_steps, a.reasoning,
             a.theses,
             s.knowledge_score, s.skills_score, s.responsibility_score,
             s.resilience_score, s.communication_score,
             i.updated_at
      from public.intv_interviews i
      join public.intv_candidates c on c.id = i.candidate_id
      join public.intv_analyses a on a.interview_id = i.id and a.is_current and a.status='completed'
      left join public.intv_scores s on s.analysis_id = a.id
      where i.status='analysis_done'
      order by i.updated_at desc
    ), rendered as (
      select
        coalesce(
          nullif(
            '🧩 Оценка по 5 тезисам:' || E'\n' ||
            (select string_agg(
                (e->>'id') || '. ' || coalesce(e->>'title','') || ' — '
                || coalesce(e->>'score','—') || '/10'
                || coalesce(': ' || nullif(left(e->>'comment',90),''),''),
                E'\n' order by (e->>'id')::int)
             from jsonb_array_elements(coalesce(theses,'[]'::jsonb)) e),
            '🧩 Оценка по 5 тезисам:' || E'\n'),
          '🧩 Тезисы: Знания '   || coalesce(knowledge_score::text,'—')
            || ' · Опыт/ArmSoft '|| coalesce(skills_score::text,'—')
            || ' · Ответств. '   || coalesce(responsibility_score::text,'—')
            || ' · Стрессоуст. ' || coalesce(resilience_score::text,'—')
            || ' · Коммуник. '   || coalesce(communication_score::text,'—')
        ) ||
        case when recommendation = 'hire' then
          E'\n' || '✅ ' || full_name || ' — ' || coalesce(interview_type,'—') || E'\n'
          || 'Рекомендация: НАНЯТЬ' || coalesce(' · итог ' || overall_score || '/10','') || E'\n'
          || 'Кратко: '  || coalesce(public.intv_first_sentence(summary),'—') || E'\n'
          || 'Сильные: ' || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_strengths,'[]'::jsonb)) t),''),'—') || E'\n'
          || 'Слабые: '  || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_weaknesses,'[]'::jsonb)) t),''),'—') || E'\n'
          || 'След. шаги: ' || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(next_steps,'[]'::jsonb)) t),''),'—') || E'\n'
          || coalesce('Обоснование: ' || public.intv_first_sentence(reasoning) || E'\n','')
          || 'Ссылка: '  || coalesce(call_url,'—')
        else
          E'\n' || '• ' || full_name || ' — ' || coalesce(interview_type,'—')
          || ' · ' || (case recommendation
                         when 'maybe'    then 'спорно 🟡'
                         when 'reject'   then 'отказать ⛔'
                         when 'training' then 'на дообучение 🎓'
                         else coalesce(recommendation,'—') end)
          || coalesce(' · итог ' || overall_score || '/10','') || E'\n'
          || 'Кратко: ' || coalesce(public.intv_first_sentence(summary),'—') || E'\n'
          || 'Ссылка: ' || coalesce(call_url,'—')
        end AS block,
        (recommendation = 'hire') as is_hire, updated_at
      from base
    )
    select block from rendered order by is_hire desc, updated_at desc
  loop
    if char_length(b) <= v_limit then
      -- Whole candidate: flush if it won't fit, then append intact.
      if v_chunk <> '' and char_length(v_chunk) + 2 + char_length(b) > v_limit then
        out_arr := out_arr || v_chunk; v_chunk := '';
      end if;
      v_chunk := case when v_chunk = '' then b else v_chunk || E'\n\n' || b end;
    else
      -- Oversized single candidate: flush, then line/word-split it (last resort).
      if v_chunk <> '' then out_arr := out_arr || v_chunk; v_chunk := ''; end if;
      for ln in select s from unnest(string_to_array(b, E'\n')) with ordinality u(s, ord) order by ord
      loop
        rest := ln;
        loop
          if char_length(rest) <= v_limit then
            piece := rest; rest := '';
          else
            piece := regexp_replace(left(rest, v_limit), '\s\S*$', '');
            if piece = '' then piece := left(rest, v_limit); end if;
            rest := ltrim(substr(rest, char_length(piece) + 1));
          end if;
          if v_chunk <> '' and char_length(v_chunk) + 1 + char_length(piece) > v_limit then
            out_arr := out_arr || v_chunk; v_chunk := '';
          end if;
          v_chunk := case when v_chunk = '' then piece else v_chunk || E'\n' || piece end;
          exit when rest = '';
        end loop;
      end loop;
      if v_chunk <> '' then out_arr := out_arr || v_chunk; v_chunk := ''; end if;
    end if;
  end loop;
  if v_chunk <> '' then out_arr := out_arr || v_chunk; end if;

  -- Header on the first message, continuation marker on the rest.
  n := coalesce(array_length(out_arr, 1), 0);
  if n = 0 then
    return array[v_header || E'\n\n—'];
  end if;
  out_arr[1] := v_header || E'\n\n' || out_arr[1];
  if n > 1 then
    for i in 2 .. n loop
      out_arr[i] := '📋 (продолжение ' || i || '/' || n || ')' || E'\n\n' || out_arr[i];
    end loop;
  end if;
  return out_arr;
end;
$$;

-- Send the report as one Telegram message per chunk. Returns the last req id.
create or replace function public.intv_send_digest()
returns bigint
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare v_tok text; v_chat text; v_req bigint; v_change timestamptz; chunks text[]; ch text;
begin
  select decrypted_secret into v_tok  from vault.decrypted_secrets where name='telegram_bot_token' limit 1;
  select decrypted_secret into v_chat from vault.decrypted_secrets where name='telegram_chat_id'   limit 1;
  if v_tok is null or v_chat is null then return null; end if;

  chunks := public.intv_digest_chunks();
  if chunks is null or array_length(chunks,1) is null then
    v_req := public.intv_tg_send(v_tok, v_chat, '📋 Обучающий центр — обработанные собеседования (0)');
  else
    foreach ch in array chunks loop
      v_req := public.intv_tg_send(v_tok, v_chat, ch);
    end loop;
  end if;

  -- Stamp the (final) request against the change it is meant to deliver, so the
  -- retry job never credits an older successful request to a newer pending change.
  select last_change_at into v_change from public.intv_notify_state where id;
  update public.intv_notify_state
     set last_request_id = v_req, last_attempt_at = now(), requested_change_at = v_change
   where id;
  return v_req;
end;
$$;

create or replace function public.intv_retry_digest()
returns void
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare st public.intv_notify_state%rowtype; v_ok boolean; v_covers boolean;
begin
  select * into st from public.intv_notify_state where id;
  if st.last_change_at is null then return; end if;
  -- Already delivered the latest change? Nothing to do.
  if st.delivered_change_at is not null and st.delivered_change_at >= st.last_change_at then
    return;
  end if;

  -- Does the last request actually cover the current pending change?
  v_covers := st.last_request_id is not null
              and st.requested_change_at is not null
              and st.requested_change_at >= st.last_change_at;
  if v_covers then
    select (status_code = 200) into v_ok from net._http_response where id = st.last_request_id;
    if v_ok then
      update public.intv_notify_state set delivered_change_at = st.requested_change_at where id;
      return;
    end if;
  end if;

  -- Either the last request failed, or a newer change arrived after it. Throttle
  -- resends to avoid hammering Telegram, then (re)send for the current change.
  if st.last_attempt_at is not null and now() - st.last_attempt_at < interval '90 seconds' then
    return;
  end if;
  perform public.intv_send_digest();
end;
$$;

create or replace function public.intv_notify_telegram()
returns trigger
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare v_tok text; v_chat text; v_name text; v_msg text; v_last timestamptz;
begin
  if NEW.status not in ('analysis_done','error') then return NEW; end if;
  if TG_OP = 'UPDATE' and NEW.status is not distinct from OLD.status then return NEW; end if;

  select decrypted_secret into v_tok  from vault.decrypted_secrets where name='telegram_bot_token' limit 1;
  select decrypted_secret into v_chat from vault.decrypted_secrets where name='telegram_chat_id'   limit 1;
  if v_tok is null or v_chat is null then return NEW; end if;

  if NEW.status = 'error' then
    select full_name into v_name from public.intv_candidates where id = NEW.candidate_id;
    v_msg := '⚠️ Ошибка обработки собеседования (Обучающий центр)' || E'\n'
          || 'Кандидат: ' || coalesce(v_name,'—') || E'\n'
          || coalesce('Причина: ' || NEW.error_message, 'Причина не указана');
    perform net.http_post(
      url := 'https://api.telegram.org/bot' || v_tok || '/sendMessage',
      headers := jsonb_build_object('Content-Type','application/json'),
      body := jsonb_build_object('chat_id', v_chat, 'text', v_msg, 'disable_web_page_preview', true),
      timeout_milliseconds := 20000);
    return NEW;
  end if;

  update public.intv_notify_state set last_change_at = now() where id;
  select last_attempt_at into v_last from public.intv_notify_state where id;
  if v_last is null or now() - v_last > interval '45 seconds' then
    perform public.intv_send_digest();
  end if;
  return NEW;
end;
$$;

-- New completed analysis rows mark a pending change so the digest is (re)sent
-- even when the interview was ALREADY analysis_done (re-analysis / direct insert)
-- and the status trigger above would not fire. The 1-minute cron does the send,
-- by which point the scores row and analysis_done status are committed.
create or replace function public.intv_analyses_mark_pending()
returns trigger
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
begin
  if NEW.status = 'completed' and NEW.is_current then
    update public.intv_notify_state set last_change_at = now() where id;
  end if;
  return NEW;
end;
$$;

drop trigger if exists trg_intv_notify_telegram on public.intv_interviews;
create trigger trg_intv_notify_telegram
  after insert or update of status on public.intv_interviews
  for each row execute function public.intv_notify_telegram();

drop trigger if exists trg_intv_analyses_notify on public.intv_analyses;
create trigger trg_intv_analyses_notify
  after insert on public.intv_analyses
  for each row execute function public.intv_analyses_mark_pending();

do $$ begin perform cron.unschedule('intv_retry_digest'); exception when others then null; end $$;
select cron.schedule('intv_retry_digest', '* * * * *', 'select public.intv_retry_digest()');
