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
-- The digest covers the latest processed participants. Hired candidates get a
-- fuller block (summary, strengths, weaknesses, next steps, reasoning); everyone
-- else gets a compact line. On 'error': a short immediate message.
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

-- Build the digest and send it as AS MANY Telegram messages as needed: each
-- candidate is a self-contained block (5-thesis breakdown + the per-candidate
-- structure), packed into ≤4096-char messages. Nothing is ever truncated; the
-- report simply spans multiple messages. Returns the LAST request id (what the
-- retry state machine tracks).
create or replace function public.intv_send_digest()
returns bigint
language plpgsql security definer
set search_path = public, vault, net, extensions
as $$
declare
  v_tok text; v_chat text; v_count int; v_req bigint; v_change timestamptz;
  v_header text; v_chunk text := ''; v_first boolean := true;
  v_limit int := 3900;   -- safety margin under Telegram's 4096 hard limit
  rec record;
begin
  select decrypted_secret into v_tok  from vault.decrypted_secrets where name='telegram_bot_token' limit 1;
  select decrypted_secret into v_chat from vault.decrypted_secrets where name='telegram_chat_id'   limit 1;
  if v_tok is null or v_chat is null then return null; end if;

  select count(*) into v_count
  from public.intv_interviews i
  join public.intv_analyses a on a.interview_id = i.id and a.is_current and a.status='completed'
  where i.status='analysis_done';

  v_header := '📋 Обучающий центр — обработанные собеседования (' || coalesce(v_count,0) || ')';

  -- One self-contained block per candidate (thesis breakdown + the previous
  -- per-candidate structure), newest/hired first. Older interviews without a
  -- theses array fall back to the compact one-line thesis summary.
  for rec in
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
    )
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
          '🧩 Оценка по 5 тезисам:' || E'\n'),  -- empty theses -> use fallback
        '🧩 Тезисы: Знания '   || coalesce(knowledge_score::text,'—')
          || ' · Опыт/ArmSoft '|| coalesce(skills_score::text,'—')
          || ' · Ответств. '   || coalesce(responsibility_score::text,'—')
          || ' · Стрессоуст. ' || coalesce(resilience_score::text,'—')
          || ' · Коммуник. '   || coalesce(communication_score::text,'—')
      ) ||
      case when recommendation = 'hire' then
        -- Hired: fuller block.
        E'\n' || '✅ ' || full_name || ' — ' || coalesce(interview_type,'—') || E'\n'
        || 'Рекомендация: НАНЯТЬ' || coalesce(' · итог ' || overall_score || '/10','') || E'\n'
        || 'Кратко: '  || coalesce(left(summary,420),'—') || E'\n'
        || 'Сильные: ' || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_strengths,'[]'::jsonb)) t),''),'—') || E'\n'
        || 'Слабые: '  || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_weaknesses,'[]'::jsonb)) t),''),'—') || E'\n'
        || 'След. шаги: ' || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(next_steps,'[]'::jsonb)) t),''),'—') || E'\n'
        || coalesce('Обоснование: ' || left(reasoning,300) || E'\n','')
        || 'Ссылка: '  || coalesce(call_url,'—')
      else
        -- Not hired: compact line.
        E'\n' || '• ' || full_name || ' — ' || coalesce(interview_type,'—')
        || ' · ' || (case recommendation
                       when 'maybe'    then 'спорно 🟡'
                       when 'reject'   then 'отказать ⛔'
                       when 'training' then 'на дообучение 🎓'
                       else coalesce(recommendation,'—') end)
        || coalesce(' · итог ' || overall_score || '/10','') || E'\n'
        || 'Кратко: ' || coalesce(left(summary,160),'—') || E'\n'
        || 'Ссылка: ' || coalesce(call_url,'—')
      end AS block,
      (recommendation = 'hire') as is_hire, updated_at
    from base
    order by is_hire desc, updated_at desc
  loop
    -- A single block should never exceed the limit, but clamp defensively.
    if char_length(rec.block) > v_limit then
      rec.block := left(rec.block, v_limit - 20) || E'\n…';
    end if;
    -- Flush the current chunk if appending this block would overflow.
    if v_chunk <> '' and char_length(v_chunk) + 2 + char_length(rec.block) > v_limit then
      v_req := public.intv_tg_send(v_tok, v_chat, v_chunk);
      v_chunk := '';
    end if;
    if v_chunk = '' then
      v_chunk := case when v_first then v_header else '📋 (продолжение)' end || E'\n\n' || rec.block;
      v_first := false;
    else
      v_chunk := v_chunk || E'\n\n' || rec.block;
    end if;
  end loop;

  -- Send the last (or only) chunk; if there were no candidates, send the header.
  if v_chunk = '' then
    v_chunk := v_header || E'\n\n—';
  end if;
  v_req := public.intv_tg_send(v_tok, v_chat, v_chunk);

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
