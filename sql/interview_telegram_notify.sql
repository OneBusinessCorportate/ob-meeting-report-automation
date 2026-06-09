-- Telegram notification on interview outcome (Supabase DB trigger + pg_net).
--
-- When an interview row reaches status 'analysis_done' or 'error', a short
-- Telegram message is sent directly from the database. The bot token + chat id
-- are read from Supabase Vault (never hard-coded), so the trigger stays SILENT
-- until those secrets are created. Apply once:
--   psql "$SUPABASE_DB_URL" -f sql/interview_telegram_notify.sql
--
-- One-time setup of the secrets (run in the Supabase SQL editor with real values):
--   select vault.create_secret('<BOT_TOKEN>', 'telegram_bot_token');
--   select vault.create_secret('<CHAT_ID>',   'telegram_chat_id');
-- To rotate later: select vault.update_secret(id, '<new value>') ...
-- To disable notifications: drop trigger trg_intv_notify_telegram on public.intv_interviews;

create extension if not exists pg_net;

create or replace function public.intv_notify_telegram()
returns trigger
language plpgsql
security definer
set search_path = public, vault, net, extensions
as $$
declare
  v_tok text; v_chat text; v_name text; v_rec text; v_score int; v_msg text;
begin
  if NEW.status not in ('analysis_done','error') then
    return NEW;
  end if;
  if TG_OP = 'UPDATE' and NEW.status is not distinct from OLD.status then
    return NEW;
  end if;

  select decrypted_secret into v_tok  from vault.decrypted_secrets where name = 'telegram_bot_token' limit 1;
  select decrypted_secret into v_chat from vault.decrypted_secrets where name = 'telegram_chat_id'   limit 1;
  if v_tok is null or v_chat is null then
    return NEW;
  end if;

  select full_name into v_name from public.intv_candidates where id = NEW.candidate_id;
  select a.recommendation, s.overall_score
    into v_rec, v_score
    from public.intv_analyses a
    left join public.intv_scores s on s.analysis_id = a.id
   where a.interview_id = NEW.id and a.is_current
   order by a.version desc
   limit 1;

  if NEW.status = 'analysis_done' then
    v_msg := '✅ Собеседование обработано (Обучающий центр)' || E'\n'
          || 'Кандидат: ' || coalesce(v_name,'—') || E'\n'
          || 'Рекомендация: ' || coalesce(v_rec,'—')
          || coalesce(' · итог ' || v_score || '/10', '') || E'\n'
          || 'Тип: ' || coalesce(NEW.interview_type,'—');
  else
    v_msg := '⚠️ Ошибка обработки собеседования (Обучающий центр)' || E'\n'
          || 'Кандидат: ' || coalesce(v_name,'—') || E'\n'
          || coalesce('Причина: ' || NEW.error_message, 'Причина не указана');
  end if;

  perform net.http_post(
    url     := 'https://api.telegram.org/bot' || v_tok || '/sendMessage',
    headers := jsonb_build_object('Content-Type','application/json'),
    body    := jsonb_build_object('chat_id', v_chat, 'text', v_msg, 'disable_web_page_preview', true)
  );
  return NEW;
end;
$$;

drop trigger if exists trg_intv_notify_telegram on public.intv_interviews;
create trigger trg_intv_notify_telegram
  after insert or update of status on public.intv_interviews
  for each row execute function public.intv_notify_telegram();
