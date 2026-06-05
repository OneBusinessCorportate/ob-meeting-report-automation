# ob-meeting-report-automation

Automates the daily processing of OneBusiness morning **accounting team
stand-up** meetings: from a **full transcript** all the way to a clean,
Russian-language **Telegram report delivered at 11:00 (Armenia time)**.

Task: `[Расшифровка утренних встреч] V. Настроить ежедневную обработку встречи,
Отправлять отчет в Telegram в 11:00`.

---

## 1. What this project does

Every working morning the pipeline:

1. **Ingests** the meeting's **full transcript** (from the Timeless API, or a
   local file fallback) and stores the raw data in Supabase **L1**.
2. **Analyzes** the full transcript with an AI model and produces a structured
   report, stored in Supabase **L2**.
3. **Delivers** the `telegram_report_md` markdown report to the management
   Telegram chat, splitting long messages automatically.

If anything is missing (no recording, no transcript, AI failure, etc.) the
pipeline **does not crash** — it records a clear status and, where relevant,
sends an understandable Telegram notification.

## 2. Architecture

```
Timeless / full transcript
        │  (Step 1 — Ingest)
        ▼
Supabase L1  →  mtg_meetings.raw_transcript   (raw layer)
        │  (Step 2 — Analyze, AI)
        ▼
Supabase L2  →  mtg_analyses                   (AI report layer)
        │  (Step 3 — Deliver)
        ▼
Telegram (markdown report, 11:00 Armenia time)
```

The existing `mtg_*` tables are reused (no parallel system is created):

| Table          | Layer | Purpose                                            |
| -------------- | ----- | -------------------------------------------------- |
| `mtg_sources`  | —     | Source registry (the `timeless` source).           |
| `mtg_meetings` | L1    | Raw meeting data; full transcript in `raw_transcript`. |
| `mtg_analyses` | L2    | AI-generated structured report + `telegram_report_md`. |

## 3. L1 vs L2

- **L1 (`mtg_meetings`) — raw layer.** Exactly what came from the source: the
  full transcript (`raw_transcript`), plus optional `raw_notes`, `raw_summary`,
  `raw_action_items`, `raw_documents`, recording URL, timestamps, etc. Deduped
  by `(source_id, source_meeting_id)`.
- **L2 (`mtg_analyses`) — AI report layer.** The derived, structured report:
  `summary`, `topics`, `action_items`, `open_questions`, `people_mentioned`,
  `problems_risks`, `sentiment`, `meeting_mood`, `mgmt_recommendations`, and the
  ready-to-send `telegram_report_md`. Linked to L1 via `meeting_id`, versioned,
  with `is_current = true` marking the latest. A DB trigger
  (`supersede_old_analyses`) automatically demotes the previous current row to
  `superseded` when a new current analysis is inserted.

## 4. Why the FULL transcript is required

> **Important correction (per boss):** the AI report MUST be generated from the
> **full raw transcript**, NOT from the Timeless TL;DR / summary.

The summary loses owners, numbers, deadlines and nuance — exactly the details a
management report needs. So:

- The full transcript is always stored in `raw_transcript.text`.
- The Timeless summary may be stored **only as additional raw data** in
  `raw_summary` / `raw_notes`. It is **never** used as a substitute for the
  transcript when generating the L2 report.
- If a real full transcript cannot be obtained, this is treated as a
  **blocker** (`transcript_not_found`) — we do not fake a transcript from the
  summary.

The AI prompt is strict and grounded: it must use only facts in the transcript,
must not invent owners / deadlines / decisions, and writes `Не указано` when
something is unclear.

## 5. Setup

```bash
git clone <repo-url>
cd ob-meeting-report-automation

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# fill in the values in .env (see section 6)
```

Requirements: Python 3.10+.

The Supabase `mtg_*` tables and the `timeless` source already exist in
production, so no schema migration is needed.

## 6. Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable                       | Required | Description                                              |
| ------------------------------ | -------- | -------------------------------------------------------- |
| `SUPABASE_URL`                 | yes      | Supabase project URL.                                    |
| `SUPABASE_SERVICE_ROLE_KEY`    | yes      | **Service-role** key (server-side only).                 |
| `TIMELESS_API_TOKEN`           | no\*     | Timeless API token. If absent, use the `--file` fallback.|
| `TIMELESS_API_BASE_URL`        | no       | Defaults to `https://api.timeless.day/v1`.               |
| `ANTHROPIC_API_KEY`            | yes      | Anthropic API key (for the AI analysis step).            |
| `AI_MODEL_ID`                  | no       | Defaults to `claude-sonnet-4-20250514`.                  |
| `AI_PROMPT_VERSION`            | no       | Defaults to `full_transcript_prompt_v1`.                 |
| `TELEGRAM_BOT_TOKEN`           | yes      | Telegram bot token.                                      |
| `TELEGRAM_MANAGEMENT_CHAT_ID`  | yes      | Target chat id for the report.                           |
| `MEETING_DEFAULT_SOURCE`       | no       | Defaults to `timeless`.                                  |
| `MEETING_DEFAULT_LANGUAGE`     | no       | Defaults to `hy` (Armenian).                             |
| `MEETING_DELIVERY_TIME`        | no       | Informational; defaults to `11:00`.                      |

\* Either a working Timeless API token **or** a local `--file` is required to
get a transcript into the pipeline.

> **Never commit real keys.** `.env` is git-ignored.

## 7. Manual run commands

Full daily pipeline (Timeless API mode):

```bash
python scripts/run_daily_meeting_report.py
```

Only ingest:

```bash
python scripts/ingest_meeting.py --file ./data/transcripts/meeting_2026_03_26.txt
```

Only analyze (one meeting):

```bash
python scripts/analyze_meeting.py --source-meeting-id timeless_manual_2026_03_26_accounting_sync
```

Only deliver:

```bash
python scripts/deliver_report.py --date 2026-03-26
```

Each script prints a JSON summary and exits non-zero on failure, which makes
logs easy to read in cron output.

## 8. Local transcript fallback mode (MVP)

When the Timeless API is unavailable, drop the full transcript into a text file
and run the pipeline against it. The rest of the flow (L1 → AI → L2 → Telegram)
works unchanged.

```bash
python scripts/run_daily_meeting_report.py \
  --file ./data/transcripts/meeting_2026_03_26.txt \
  --title "Утренняя планёрка бухгалтерии" \
  --date 2026-03-26
```

- The file content is stored verbatim in `raw_transcript.text`
  (`type: "full_transcript"`).
- `source_meeting_id` defaults to `manual_<YYYY_MM_DD>`; override with
  `--source-meeting-id` to match an existing record.
- A ready-to-use sample lives at `data/transcripts/sample_meeting.txt`.

## 9. Render Cron setup

`render.yaml` defines one cron service that runs the combined pipeline.

> **Timezone:** Render cron runs in **UTC**. Armenia is **UTC+4** (no DST), so
> **11:00 Armenia = 07:00 UTC**. The schedule `0 7 * * 1-5` therefore fires at
> 11:00 Armenia time, Monday–Friday.

Steps:

1. Create a new **Cron Job** on Render from this repo (or use the Blueprint /
   `render.yaml`).
2. Build command: `pip install -r requirements.txt`.
3. Start command: `python scripts/run_daily_meeting_report.py`.
4. Add the environment variables from section 6 (mark secrets as
   not-synced / set them in the dashboard).
5. Confirm the schedule is `0 7 * * 1-5`.

`render.yaml` also contains a commented-out alternative with three separate
cron jobs (ingest ~10:45, analyze ~10:50, deliver 11:00) if you prefer to split
the steps. One combined job is fine for the MVP.

## 10. Troubleshooting

**Transcript not found** (`transcript_not_found` / `recording_not_found`)
- Cause: Timeless returned no meeting/transcript, or the local file was missing
  or empty.
- Effect: no L1 row with a usable transcript; the deliver step sends
  `Запись/отчёт за сегодня не найден.`
- Fix: verify the meeting exists in Timeless, or supply `--file` with the full
  transcript.

**Timeless API not available**
- Symptom: log shows `Timeless API not configured or full transcript endpoint
  unavailable`.
- This is treated as a **blocker** by design — we never fabricate a transcript
  from the summary. Use the local `--file` fallback so the rest of the pipeline
  still runs.

**AI failed**
- Causes: API/network error, or the model returned invalid JSON.
- Effect: an L2 row is stored with `status = failed` and an `error_message`
  (the pipeline does not crash). The deliver step then reports no current
  report for the day.
- Fix: check `ANTHROPIC_API_KEY` / model id, inspect `error_message`, and
  re-run `scripts/analyze_meeting.py`.

**Telegram failed**
- Causes: bad `TELEGRAM_BOT_TOKEN` / chat id, the bot is not a member of the
  chat, or Markdown parsing errors.
- Effect: `deliver` returns `delivery_failed`; the failure detail is recorded in
  the analysis `ai_metadata.delivery`.
- Fix: verify the token/chat id and that the bot can post to the chat; long
  messages are split automatically at ~4000 chars.

## 11. Tests

Offline tests (no network, no real keys) cover JSON parsing, message splitting,
ingest, analyze (success / AI failure / bad JSON / missing transcript) and
delivery (present / missing report):

```bash
python -m pytest tests/ -v
# or, without pytest:
python tests/test_basic_flow.py
```

## 12. Definition of Done (DoD)

- [x] Repository has all project files (package, scripts, tests, config).
- [x] README is complete.
- [x] `.env.example` is complete.
- [x] Scripts exist for ingest / analyze / deliver / full daily run.
- [x] Local transcript fallback works.
- [x] Supabase L1 insert/upsert works (deduped by `source_id + source_meeting_id`).
- [x] AI L2 insert works (versioned, `is_current`).
- [x] Telegram delivery works (markdown + long-message splitting).
- [x] Missing transcript case is handled without crashing.
- [x] Render cron config exists (`0 7 * * 1-5` = 11:00 Armenia).
- [x] Logs are clear.
- [x] No real secrets are committed (`.env` is git-ignored).
