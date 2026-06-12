-- Once-per-day Telegram send log for the daily meeting report.
--
-- meeting_pipeline/deliver.py atomically claims a (report_date, kind) row here
-- BEFORE sending to Telegram, so however many times the cron job fires in a
-- day (manual re-runs, a mis-set schedule on Render), each message — the
-- report itself or the "report not found" notice — goes out at most once.
-- A failed send releases (deletes) the claim so the next run can retry.
--
-- Kinds used by the code:
--   'missing_report_notice'  — the "Запись/отчёт за сегодня не найден." notice
--   'report:<analysis_id>'   — delivery of a specific L2 report version
--     (keyed by analysis id so a forced re-analysis can still go out same day)

create table if not exists public.mtg_delivery_log (
  report_date date        not null,
  kind        text        not null,
  sent_at     timestamptz not null default now(),
  primary key (report_date, kind)
);

-- Service-role key bypasses RLS; enabling it keeps the table closed to anon.
alter table public.mtg_delivery_log enable row level security;
