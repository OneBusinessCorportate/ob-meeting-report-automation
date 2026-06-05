# ob-meeting-report-automation

OneBusiness transcription automation. This repository hosts **two related
pipelines** that share the same Timeless + Supabase + config infrastructure:

- **Task I — Morning meeting report** (`meeting_pipeline/`): daily morning
  accounting stand-up → full transcript → Supabase L1 → AI report → Supabase L2
  → Russian **Telegram report at 11:00 (Armenia time)**.
  Task: `[Расшифровка утренних встреч] V. Настроить ежедневную обработку встречи,
  Отправлять отчет в Telegram в 11:00`.
- **Task II — Interview/onboarding transcription** (`interview_pipeline/`):
  interview & onboarding call **links** (from the «Обучающий центр ОВ» table) →
  fetch **full transcript** via Timeless (link → transcript) → save it correctly
  in Supabase `interview_calls` → update processing status.
  Task: `[Автоматизация транскрибации собеседований] II из таблицы «Обучающий
  центр ОВ»`.

Both use the **full transcript** (never a summary), never crash on missing
data, and support a local-file fallback when the Timeless API is unavailable.

> Sections 1–12 below document **Task I**. **Task II** is documented in
> section 13.

---

## 1. What this project does (Task I)

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
  `problems_risks`, `sentiment`, `meeting_mood`, `late_start` /
  `late_start_minutes`, `mgmt_recommendations`, and the ready-to-send
  `telegram_report_md`. Linked to L1 via `meeting_id`, versioned, with
  `is_current = true` marking the latest. A DB trigger
  (`supersede_old_analyses`) automatically demotes the previous current row to
  `superseded` when a new current analysis is inserted.

### What the L2 report contains

Per the management requirements, each report (and the Telegram message) covers:

- **summary** — short recap.
- **topics** — discussed topics with key points and rough time share.
- **decisions** — a decision log (only explicitly stated decisions).
- **action_items** — task, assignee, deadline, priority (`Не указано` when not stated).
- **open_questions** — unresolved questions.
- **people_mentioned** — who spoke / who was mentioned, with context.
- **praised / criticized** — who was praised and who was criticized, and why.
- **problems_risks** — problems and risks with severity.
- **sentiment** + **meeting_mood** — overall tone, energy, engagement, dominant/silent speakers.
- **late_start / late_start_minutes** — whether the meeting started late, and by how much.
- **mgmt_recommendations** — a **separate manager briefing for Эмилия**:
  `focus_points`, `recurring_issues`, `risks`, `who_to_support`,
  `needs_intervention`.
- **telegram_report_md** — the final markdown message.

`decisions`, `praised` and `criticized` have no dedicated column, so they are
preserved inside `mtg_analyses.ai_metadata.report_extras` (and surfaced in the
Telegram report). Nothing is lost.

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

Re-analyze (force a new L2 version even if a current report exists):

```bash
python scripts/analyze_meeting.py --source-meeting-id timeless_manual_2026_03_26_accounting_sync --force
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
  messages are split automatically at ~4000 chars. The stored report uses GFM
  `**bold**`, which the client converts to Telegram legacy `*bold*` on send. If
  the Markdown still can't be parsed (HTTP 400), the client automatically
  retries that part as **plain text** so the report is never dropped.

**Duplicate L2 reports**
- The pipeline is safe to rerun: meetings that already have a *current
  completed* L2 report are skipped, so no duplicate versions are created.
- To intentionally regenerate a report, pass `--force` to `analyze_meeting.py`
  or `run_daily_meeting_report.py`; this inserts a new version and supersedes
  the old one via the DB trigger.

## 11. Tests

Offline tests (no network, no real keys) cover JSON parsing, message splitting,
ingest, analyze (success / AI failure / bad JSON / missing transcript),
delivery (present / missing report) and the interview pipeline (task II):

```bash
python -m pytest tests/ -v
# or, without pytest:
python tests/test_basic_flow.py
python tests/test_interview_flow.py
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
- [x] Safe to rerun — no duplicate L2 versions (idempotent; `--force` to override).
- [x] One failing step does not crash the run (each step is isolated).
- [x] Telegram Markdown failures fall back to plain text instead of dropping the report.
- [x] L2 includes decision log, praise/criticism, late-start, and a manager briefing.

---

## 13. Task II — interview / onboarding transcription (`interview_transcript_processing`)

This repository supports **two independent modes**, kept separate because the
meeting type and purpose differ:

| Mode | Code | Flow | Output |
| ---- | ---- | ---- | ------ |
| `daily_meeting_report` | `meeting_pipeline/` | transcript → L1 → AI L2 → Telegram | Telegram report at 11:00 |
| `interview_transcript_processing` | `interview_pipeline/` | call link → full transcript → saved transcript + status | Saved transcript + per-link status (no Telegram) |

Task II automates fetching **full transcripts** for interview & onboarding/
training calls (the «Обучающий центр ОВ» table). **Hiring decisions are made on
these calls, so transcripts are saved in full and correctly — never a summary.**
No AI report or Telegram message is produced (that can be added later).

### Automation logic

```
Interview/onboarding call link             (from «Обучающий центр ОВ»)
        │   load links: --input/--csv (export) | --url | INTERVIEW_LINKS_TABLE
        ▼
Check transcript availability → fetch FULL transcript via Timeless
        │                        (local transcript_file fallback for MVP)
        ▼
Supabase  →  interview_calls.raw_transcript  (full transcript, saved correctly)
        ▼
Per-link status saved in the table AND exported as a status-list CSV
```

- Links are processed **one by one**; each gets a logged status, and one bad
  link never stops the batch.
- Each link gets a stable `source_call_id` (from the Timeless URL), so the table
  is **deduped** and the job is **safe to rerun** — calls already `saved` are
  skipped unless `--force`.
- Reuses the shared `mtg_sources` registry (`timeless`) — no duplicate source.

### Statuses

| Status | Meaning |
| ------ | ------- |
| `pending` | Row created, not yet processed. |
| `transcript_found` | A full transcript was located for the call. |
| `saved` | Full transcript fetched and stored in `raw_transcript`. |
| `transcript_not_available` | Recording/link exists but no full transcript could be fetched. |
| `manual_action_required` | Cannot fetch automatically (no API token and no local file) — a human must supply it. |
| `failed` | Unexpected error (recorded in `error_message`). |

### One-time setup (create the table)

Apply the migration once (it reuses the existing `update_updated_at()` trigger
from the `mtg_` tables):

```bash
psql "$SUPABASE_DB_URL" -f sql/interview_calls.sql
# or paste sql/interview_calls.sql into the Supabase SQL editor
```

### Environment variables (in addition to Task I)

| Variable                 | Required | Description                                              |
| ------------------------ | -------- | -------------------------------------------------------- |
| `INTERVIEW_CALLS_TABLE`  | no       | Target table. Defaults to `interview_calls`.             |
| `INTERVIEW_LINKS_TABLE`  | no       | Supabase table of links (if not using `--input` / `--url`).|
| `INTERVIEW_DEFAULT_ROLE` | no       | Default role label. Defaults to `бухгалтер`.             |

Supabase + Timeless variables are shared with Task I.

### Run commands

```bash
# Main entry — from a spreadsheet/CSV exported from «Обучающий центр ОВ»:
python scripts/process_training_center_links.py --input ./data/training_center_links.csv

# One or more links directly:
python scripts/process_training_center_links.py \
  --url https://app.timeless.day/meetings/abc123 \
  --url https://app.timeless.day/meetings/def456

# Choose where to write the status list, and re-process already-saved calls:
python scripts/process_training_center_links.py \
  --input ./data/training_center_links.csv \
  --output ./data/interviews/status.csv --force
```

A per-link **status list** is printed to the log and written to a CSV
(`--output`, default `./data/interviews/status_<timestamp>.csv`).
`scripts/transcribe_interviews.py` is kept as a thin alias of the same logic.

### Input format (spreadsheet / CSV export)

Header row; only `call_url` is required:

```
call_url,candidate_name,role,call_type,source_call_id,transcript_file
https://app.timeless.day/meetings/c1,Иван,бухгалтер,interview,c1,
```

- `transcript_file` (optional) — path to a local full-transcript text file used
  as the **MVP fallback** when the Timeless API can't return the transcript.
- A working sample is `data/training_center_links.example.csv` (with a sample
  transcript in `data/interviews/`).

### Troubleshooting (Task II)

- **No transcript & no token** → `manual_action_required`: configure
  `TIMELESS_API_TOKEN` or add a `transcript_file` column for that row.
- **Timeless returned no transcript** → `transcript_not_available` (with a clear
  `error_message`). We never substitute the Timeless summary.
- **Wrong id extracted from a link** — set `source_call_id` explicitly in the CSV.
- **Table missing** — run `sql/interview_calls.sql` first.

### Blockers (Task II)

- The «Обучающий центр ОВ» source table is in **Notion**, which this system
  cannot read directly. Links are provided via `--input` (export), `--url`, or a
  mirrored Supabase table (`INTERVIEW_LINKS_TABLE`). Writing statuses *back into
  Notion* is out of scope — statuses live in `interview_calls` and the exported
  status CSV.
- Automatic full-transcript retrieval depends on a working **Timeless API**
  token + transcript endpoint. Until confirmed, use the local `transcript_file`
  fallback. (We do not scrape the Timeless UI.)

### DoD (Task II)

- [x] Spreadsheet/table of links is analyzed (loaded from CSV/URL/Supabase).
- [x] Interview & onboarding calls processed (`call_type` = interview/onboarding).
- [x] Transcript availability is checked per link.
- [x] Full transcripts saved when available (`raw_transcript`, never a summary).
- [x] Status saved in `interview_calls` AND exported as a status-list CSV.
- [x] Tested on multiple calls; one failing link doesn't crash the batch.
- [x] README explains how to run it (this section).
- [x] Missing-transcript cases handled clearly (`transcript_not_available` /
      `manual_action_required`).
- [x] Offline tests (`tests/test_interview_flow.py`).
