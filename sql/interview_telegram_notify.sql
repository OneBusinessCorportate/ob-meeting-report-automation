-- Telegram DIGEST notification on interview outcomes (Supabase trigger + pg_net).
--
-- On status -> 'analysis_done' the DB sends ONE digest message covering the
-- latest processed participants (name, type, recommendation, score, short
-- summary, strengths, weaknesses, link). A debounce (intv_notify_state) collapses
-- a batch into a single message. On 'error' it sends a short error message.
--
-- Secrets come from Supabase Vault (telegram_bot_token / telegram_chat_id) — ASCII
-- only, no Cyrillic. Stays silent until they exist.
--
-- NOTE: delivery depends on Supabase egress to api.telegram.org. If sends time out
-- intermittently (pg_net TLS handshake hang), prefer sending from the application
-- layer (Render) instead — see interview_pipeline/report.py.

create extension if not exists pg_net;

create table if not exists public.intv_notify_state (
  id boolean primary key default true,
  last_digest_at timestamptz,
  constraint intv_notify_state_singleton check (id)
);
insert into public.intv_notify_state(id) values (true) on conflict do nothing;

create or replace function public.intv_notify_telegram()
returns trigger
language plpgsql
security definer
set search_path = public, vault, net, extensions
as $$
declare
  v_tok text; v_chat text; v_name text; v_body text; v_count int; v_msg text; v_last timestamptz;
begin
  if NEW.status not in ('analysis_done','error') then return NEW; end if;
  if TG_OP = 'UPDATE' and NEW.status is not distinct from OLD.status then return NEW; end if;

  select decrypted_secret into v_tok  from vault.decrypted_secrets where name = 'telegram_bot_token' limit 1;
  select decrypted_secret into v_chat from vault.decrypted_secrets where name = 'telegram_chat_id'   limit 1;
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

  select last_digest_at into v_last from public.intv_notify_state where id;
  if v_last is not null and now() - v_last < interval '45 seconds' then return NEW; end if;
  update public.intv_notify_state set last_digest_at = now() where id;

  with base as (
    select i.id, c.full_name, i.interview_type, i.call_url, a.recommendation, s.overall_score,
           a.summary, a.candidate_strengths, a.candidate_weaknesses, i.updated_at
    from public.intv_interviews i
    join public.intv_candidates c on c.id = i.candidate_id
    join public.intv_analyses a on a.interview_id = i.id and a.is_current and a.status = 'completed'
    left join public.intv_scores s on s.analysis_id = a.id
    where i.status = 'analysis_done'
    order by i.updated_at desc limit 8
  )
  select count(*),
    string_agg(
      '• ' || full_name || ' — ' || coalesce(interview_type,'—') || E'\n'
      || 'Рекомендация: ' || coalesce(recommendation,'—') || coalesce(' · итог ' || overall_score || '/10','') || E'\n'
      || 'Сильные: ' || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_strengths,'[]'::jsonb)) t),''),'—') || E'\n'
      || 'Слабые: '  || coalesce(nullif((select string_agg(t,'; ') from jsonb_array_elements_text(coalesce(candidate_weaknesses,'[]'::jsonb)) t),''),'—') || E'\n'
      || 'Кратко: '  || coalesce(left(summary,280),'—') || E'\n'
      || 'Ссылка: '  || coalesce(call_url,'—'),
      E'\n\n' order by updated_at desc)
  into v_count, v_body from base;

  v_msg := '📋 Обучающий центр — обработанные собеседования (' || coalesce(v_count,0) || ')' || E'\n\n' || coalesce(v_body,'—');

  perform net.http_post(
    url := 'https://api.telegram.org/bot' || v_tok || '/sendMessage',
    headers := jsonb_build_object('Content-Type','application/json'),
    body := jsonb_build_object('chat_id', v_chat, 'text', v_msg, 'disable_web_page_preview', true),
    timeout_milliseconds := 20000);
  return NEW;
end;
$$;

drop trigger if exists trg_intv_notify_telegram on public.intv_interviews;
create trigger trg_intv_notify_telegram
  after insert or update of status on public.intv_interviews
  for each row execute function public.intv_notify_telegram();
