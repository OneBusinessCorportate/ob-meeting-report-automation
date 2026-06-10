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
  the `effectiveness` score (1–10), `summary`, `topics`, `action_items`,
  `open_questions`, `people_mentioned`, `problems_risks`, `sentiment`,
  `meeting_mood`, `late_start` / `late_start_minutes`, `mgmt_recommendations`
  (advice for Эмилия), and the ready-to-send `telegram_report_md`. Linked to L1 via `meeting_id`, versioned, with
  `is_current = true` marking the latest. A DB trigger
  (`supersede_old_analyses`) automatically demotes the previous current row to
  `superseded` when a new current analysis is inserted.

### What the L2 report contains

Per the management requirements, each report (and the Telegram message) covers:

- **effectiveness** — the headline meeting score on a 1–10 scale (8+ = a good
  meeting). The agent rates the meeting against a checklist of what a good
  meeting must have (per Lilit): every employee spoke up, and Эмилия asked
  questions, set tasks, shared news and praised someone. Each criterion gets a
  `status` (выполнено / частично / не выполнено). The score is shown at the very
  top of the Telegram report so you can see at a glance what kind of meeting it
  was.
- **summary** — short recap (1-2 sentences, no "Кратко" label).
- **participant_breakdown** — every accountant by name: what was done yesterday,
  today's plan, which cases, blockers and where help is needed. Anyone on the
  roster (`MEETING_TEAM_ROSTER`) who said nothing is flagged
  `participated: false` ("не принимал(а) участия").
- **manager_reactions** — Эмилия's reactions to each accountant
  (рекомендация / критика / задача), plus `followup_on_previous_tasks` (did she
  ask for results on tasks set earlier?) and `who_took_ownership`.
- **talk_share** — rough talk-time split (manager % vs accountants %).
- **topics** — discussed topics with key points and rough time share.
- **decisions** — a decision log (only explicitly stated decisions).
- **action_items** — task, assignee, deadline, priority, and `how_to_track`.
- **problems_risks** — situations described in context, with severity, owner,
  deadline and how to track progress.
- **open_questions** — unresolved questions.
- **people_mentioned** — who spoke / who was mentioned, with context.
- **praised / criticized** — who was praised and who was criticized, and why.
- **sentiment** + **meeting_mood** — overall tone, energy, engagement, dominant/silent speakers.
- **late_start / late_start_minutes** — whether the meeting started late, and by how much.
- **mgmt_recommendations** — recommendations addressed to **Эмилия** (not the
  team) on how to run meetings more effectively, derived from where the
  effectiveness checklist fell short (`what_went_well`, `what_to_improve`,
  `recommendations`). Stored for now, but **not** rendered into the Telegram
  report (relevance still being worked out with Lilit).
- **telegram_report_md** — the final message, written as clean plain text
  (emoji headers, `•` bullets, **no** markdown `*`/`_`/`[]` characters) so it
  reads cleanly in Telegram without stray asterisks.

`effectiveness`, `decisions`, `praised`, `criticized`, `participant_breakdown`,
`manager_reactions`, `followup_on_previous_tasks`, `who_took_ownership` and
`talk_share` have no dedicated column, so they are preserved inside
`mtg_analyses.ai_metadata.report_extras` (and surfaced in the Telegram report).
The effectiveness score is therefore queryable for trend tracking via
`ai_metadata->'report_extras'->'effectiveness'->>'score'`. Nothing is lost.

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
| `AI_PROVIDER`                  | no       | `anthropic` (default) or `gemini`.                       |
| `ANTHROPIC_API_KEY`            | yes\*\*  | Anthropic API key. Required when `AI_PROVIDER=anthropic`.|
| `GEMINI_API_KEY`               | yes\*\*  | Google Gemini API key. Required when `AI_PROVIDER=gemini`.|
| `AI_MODEL_ID`                  | no       | Per-provider default: `claude-sonnet-4-20250514` / `gemini-2.5-pro`. |
| `AI_PROMPT_VERSION`            | no       | Defaults to `full_transcript_prompt_v2`.                 |
| `MEETING_TEAM_ROSTER`          | no       | Known accountants (`Имя:роль`, comma-separated) so the report covers everyone and flags non-participants. |
| `TELEGRAM_BOT_TOKEN`           | yes      | Telegram bot token.                                      |
| `TELEGRAM_MANAGEMENT_CHAT_ID`  | yes      | Target chat id for the report.                           |
| `MEETING_DEFAULT_SOURCE`       | no       | Defaults to `timeless`.                                  |
| `MEETING_DEFAULT_LANGUAGE`     | no       | Defaults to `hy` (Armenian).                             |
| `MEETING_DELIVERY_TIME`        | no       | Informational; defaults to `11:00`.                      |

\* Either a working Timeless API token **or** a local `--file` is required to
get a transcript into the pipeline.

\*\* Exactly one AI key is required, matching `AI_PROVIDER`: `ANTHROPIC_API_KEY`
for `anthropic`, or `GEMINI_API_KEY` for `gemini`.

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

Check Timeless API connectivity and discover the real endpoint shapes
(uses `TIMELESS_API_TOKEN`; the token is never printed):

```bash
python scripts/check_timeless.py                 # probe listing + transcript endpoints
python scripts/check_timeless.py --meeting-id ID # test transcript endpoints for a known id
```

It reports which paths return HTTP 200 and their top-level JSON keys. If the real
API differs from the defaults, set `TIMELESS_MEETINGS_PATH`,
`TIMELESS_TRANSCRIPT_PATH_TEMPLATES`, and/or `TIMELESS_AUTH_SCHEME` (no code change
needed). The client retries transient errors (network / 429 / 5xx) with backoff —
tune with `TIMELESS_MAX_RETRIES` — and follows cursor/page pagination on listings.

**Debug "Timeless returned 0 meeting(s)"** — when the listing succeeds (HTTP 200)
but comes back empty even though meetings exist in the Timeless UI, run the safe,
read-only debugger (GET only, **never prints the token**):

```bash
python scripts/debug_timeless_api.py --start-date 2026-05-26 --end-date 2026-06-09
python scripts/debug_timeless_api.py --days-back 14
```

It reports whether the token is present (length only), the exact URL/status, the
raw response keys + detected list key + meeting count, any pagination markers,
and then **tries a matrix of variants** — the configured params, the same params
without the status filter, no params, and every common date param-name pair
(`from`/`to`, `start`/`end`, `created_after`/`created_before`, `since`/`until`,
…). If auth is rejected it also probes the three auth schemes. The final
`diagnosis` line tells you exactly which env var to set:

- A variant returned meetings → set `TIMELESS_START_PARAM`/`TIMELESS_END_PARAM`
  (and/or clear `TIMELESS_STATUS_FILTER=`) to match it.
- Auth was rejected → fix `TIMELESS_AUTH_SCHEME` or re-issue the token.
- Every variant returned 200 + 0 meetings → the API genuinely exposes no
  meetings for this token/workspace/date range (see Troubleshooting below).

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

**Timeless returns 0 meetings (but meetings exist in the UI)**
- Symptom: the run is otherwise healthy (Supabase connects, Telegram sends
  "report not found"), but the log shows
  `Timeless returned 0 meeting(s) for <start>..<end>` and the ingest result is
  `recording_not_found`.
- The endpoint itself is confirmed reachable: `GET https://api.timeless.day/v1/meetings`
  responds `401` without a valid token (it is *not* a 404), so a 200-but-empty
  response means **auth is fine and the query is returning nothing** — usually a
  param-shape mismatch, the status filter, or a workspace/date-range issue, not a
  wrong URL.
- Debug it without leaking the token:

  ```bash
  python scripts/debug_timeless_api.py --start-date 2026-05-26 --end-date 2026-06-09
  ```

  The report ends with a `result_code` + `diagnosis`. Act on the code:
  - `no_meetings_in_range` → **not a bug.** `start_date`/`end_date` are honoured
    and there are simply no meetings in the requested dates (the API ignores every
    *other* param name, so any variant that returns the same count as "no params"
    is being ignored, not filtering). Re-run ingest with the dates that actually
    have meetings. *(This is the real cause of the OneBusiness 0-meeting reports:
    the only meetings in the workspace are from March 2026, so a May/June or
    "today" range correctly returns 0.)*
  - `wrong_param` → a date param name **other** than `start_date`/`end_date`
    returned a real filtered subset. Set `TIMELESS_START_PARAM` /
    `TIMELESS_END_PARAM` to that pair in Render. No code change needed.
  - `status_filter` → the same date range returns meetings only without the
    status filter. Set `TIMELESS_STATUS_FILTER=` (empty) or the correct value.
  - `auth_wrong_scheme` / `auth_blocker` → set `TIMELESS_AUTH_SCHEME`
    (`bearer` | `x-api-key` | `token`) to the accepted scheme, or re-issue the
    token if all schemes 401/403.
  - `empty_workspace` → the documented blocker:
    `Timeless API does not expose meetings for this token/workspace/date range`.
    Confirm the meetings belong to **this token's workspace**, that the token has
    API access to them, and that the date range matches the meeting dates
    (Armenia is UTC+4 — a UTC range can miss edge-of-day meetings). Until the
    API exposes them, use the local `--file` fallback.

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

---

## 14. «Обучающий центр / анализ собеседований» — full interview analysis

This is the complete project on top of Task II: read the candidate/interview
table from the **Google Sheet «Обучающий центр ОВ»**, fetch the **full
transcript** per interview, store everything in **Supabase**, and run an **AI
analysis** of each candidate (Armenian-aware, Russian output) with a hire /
maybe / reject / training recommendation and scores.

Code lives in `interview_pipeline/` (the analysis modules) and reuses the shared
`meeting_pipeline/` infra (config, Timeless client, AI provider, Telegram).

### 14.1 Architecture

```
Google Sheet «Бух»  (candidates + interview links)
   │  sheet_source.py  — Google Sheets API | published CSV | local .xlsx/.csv
   ▼
intv_candidates  +  intv_interviews        (deduped; status state machine)
   │  transcript_resolver.py — Timeless API → Google Docs → manual file
   ▼
intv_transcripts  (raw_text + cleaned_text)  +  intv_transcript_segments
   │  analyze.py — AI (Anthropic/Gemini), strict-JSON interview prompt
   ▼
intv_analyses  (summary, strengths, weaknesses, recommendation, …)  +  intv_scores
   │  report.py (optional) — short Russian Telegram report per interview
   ▼
intv_sync_logs  (every stage logged; safe to rerun)
```

### 14.2 Database schema

Defined in **`sql/interview_analysis_schema.sql`** (already applied to the
**OB FAQ** Supabase project). Tables (namespaced `intv_` so they live safely
beside the existing `kb_`/`mtg_`/`rag_` tables):

| Table | Purpose | Key columns |
| ----- | ------- | ----------- |
| `intv_candidates` | one person per hiring track | `full_name`, `normalized_name`, `track`, `role`, `sheet_status`, `test_score`, dates; **UNIQUE(track, normalized_name)** |
| `intv_interviews` | one interview/onboarding call | `candidate_id`→candidates, `source_id`→`mtg_sources`, `call_url`, `transcript_source`, `source_call_id`, `status`; **UNIQUE(source_id, source_call_id)** |
| `intv_transcripts` | raw kept SEPARATE from cleaned | `interview_id`, `raw_text`, `cleaned_text`, `raw_payload`, counts; **UNIQUE(interview_id)** |
| `intv_transcript_segments` | diarized lines | `transcript_id`, `interview_id`, `idx`, `speaker`, `start_ms`, `end_ms`, `text` |
| `intv_analyses` | AI assessment, versioned | `interview_id`, `candidate_id`, `version`, `is_current`, `summary`, `candidate_strengths/weaknesses`, `red_flags`, `next_steps`, `recommendation`, `reasoning` |
| `intv_scores` | numeric scores (0–10), 1:1 with an analysis | `analysis_id`, `communication/professional/motivation/overall_score`; CHECK 0–10 |
| `intv_sync_logs` | per-stage run log | `run_id`, `stage`, `level`, `status`, `interview_id`, `message`, `detail` |

`intv_analyses` is versioned: inserting a new `is_current` row demotes the
previous one to `superseded` via the **BEFORE INSERT** trigger
`intv_supersede_old_analyses`, and `uq_intv_analyses_current` guarantees one
current analysis per interview.

### 14.3 Interview status state machine

`intv_interviews.status`:

| Status | Meaning |
| ------ | ------- |
| `new` | Row created, not yet processed. |
| `link_missing` | Candidate row has no interview/transcript link. |
| `transcript_pending` | Fetching the full transcript. |
| `transcript_ready` | Full transcript fetched + stored (raw + cleaned). |
| `analysis_pending` | AI analysis in progress. |
| `analysis_done` | Analysis + scores stored. ✅ terminal |
| `error` | Transcript could not be fetched, or AI failed (see `error_message`). |

### 14.4 Transcript source order

Per the requirement *"try Timeless, if nothing use Docs"*:
1. explicit local file (`transcript_file` column / MVP fallback);
2. **Timeless API** (when `TIMELESS_API_TOKEN` is set);
3. **Google Docs / Drive** link from the sheet (the transcripts we actually have
   today are Google Docs) — via the Google API, or a public export URL;
4. otherwise `error` with a clear message. We never substitute a summary.

### 14.5 The AI analysis prompt

`interview_pipeline/prompts/interview_analysis_v1.py`. Grounded, Armenian-aware,
**always answers in Russian**, returns STRICTLY this JSON (scores 0–10):

```json
{
  "transcript_language": "hy",
  "summary": "...", "summary_original": "...",
  "candidate_strengths": [], "candidate_weaknesses": [],
  "communication_score": 0, "professional_score": 0,
  "motivation_score": 0, "overall_score": 0,
  "recommendation": "hire|maybe|reject|training",
  "reasoning": "...", "red_flags": [], "next_steps": []
}
```

The code normalises the result (scores clamped to 0–10; `recommendation` mapped
to the four canonical values, incl. Russian synonyms) so a slightly-off model
response still stores cleanly instead of crashing.

### 14.6 Setup

1. Apply the schema once (already done on OB FAQ; for a fresh project):
   ```bash
   psql "$SUPABASE_DB_URL" -f sql/interview_analysis_schema.sql
   ```
2. Choose how the sheet is read and set the env vars (section 14.8):
   - **Google Sheets API** (most automatic): create a Google Cloud **service
     account**, enable the Sheets + Drive APIs, download the JSON key, and
     **share the spreadsheet (and the transcript Google Docs) with the service
     account's email** (read access). Set `INTERVIEW_SPREADSHEET_ID` +
     `GOOGLE_SERVICE_ACCOUNT_FILE`/`_JSON`.
   - **Published CSV**: File → Share → Publish the «Бух» tab to web as CSV, set
     `INTERVIEW_SHEET_CSV_URL`.
   - **Local file**: download the sheet as `.xlsx` and pass `--xlsx`.
3. Set the AI key (`ANTHROPIC_API_KEY` or `GEMINI_API_KEY` + `AI_PROVIDER`) and
   `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`.

### 14.7 Run commands

```bash
# Full sync from the configured Google Sheet (fetch transcript + analyze + store):
python scripts/sync_interviews.py

# From a local .xlsx export of the sheet (any tab; default «Бух»):
python scripts/sync_interviews.py --xlsx ./data/training_center.xlsx --tab Бух

# Only fetch + store transcripts, skip AI analysis:
python scripts/sync_interviews.py --xlsx ./data/training_center.xlsx --no-analyze

# Re-process finished interviews (creates a new analysis version):
python scripts/sync_interviews.py --force

# Also send a short Russian Telegram report per analyzed interview:
python scripts/sync_interviews.py --deliver
```

The script prints a JSON summary (`processed`, `analysis_done`, `errors`,
per-status `counts`, `run_id`). Every step is also written to `intv_sync_logs`.

### 14.8 Environment variables (in addition to sections 6)

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `INTERVIEW_SPREADSHEET_ID` | for Google API | Google Sheet id. |
| `INTERVIEW_SHEET_TABS` | no | Tabs to read (default `Бух`). |
| `INTERVIEW_SHEET_CSV_URL` | alt. | Published-to-web CSV export URL. |
| `INTERVIEW_LOCAL_XLSX` | alt. | Path to a local `.xlsx` export. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` / `_FILE` | for Google API | Service-account key (blob or path). |
| `INTERVIEW_ANALYSIS_ENABLED` | no | Run AI analysis (default `true`). |
| `INTERVIEW_TELEGRAM_ENABLED` | no | Send Telegram report (default `false`). |
| `INTERVIEW_TELEGRAM_CHAT_ID` | no | Dedicated chat (else management chat). |
| `INTERVIEW_ANALYSIS_PROMPT_VERSION` | no | Default `interview_analysis_v1`. |

Supabase / Timeless / AI variables are shared with the rest of the project.

### 14.9 Reliability

- **No duplicates** — candidates deduped by `(track, normalized_name)`,
  interviews by `(source_id, source_call_id)`. Safe to rerun.
- **Idempotent** — interviews already `analysis_done` are skipped unless
  `--force`; re-analysis is versioned (`is_current`), nothing is lost.
- **Raw ≠ processed** — `raw_text` is stored verbatim; cleaning lives in
  `cleaned_text`, so analysis can always be re-run from the raw layer.
- **Never crashes** — each candidate is isolated; a missing link →
  `link_missing`, an unfetchable transcript → `error`, an AI failure → a single
  `failed` analysis row + `error` status, all logged. One bad row never stops
  the batch.

### 14.10 How to verify

```bash
# Offline tests (no network/keys):
python tests/test_interview_analysis_flow.py

# After a real run, in Supabase (OB FAQ):
select status, count(*) from intv_interviews group by status;
select c.full_name, a.recommendation, s.overall_score
  from intv_analyses a
  join intv_candidates c on c.id = a.candidate_id
  left join intv_scores s on s.analysis_id = a.id
 where a.is_current order by s.overall_score desc nulls last;
select * from intv_sync_logs order by created_at desc limit 20;
```

### 14.11 What I still need from you

- **Google access** — a service-account JSON **and** the «Обучающий центр ОВ»
  spreadsheet + the transcript Google Docs **shared** with that service
  account's email (read). (Or a published CSV URL + publicly-viewable docs.)
- **Timeless** — if interviews are also recorded in Timeless, a working
  `TIMELESS_API_TOKEN`; otherwise the pipeline uses the Google-Doc links.
- **AI key** — `ANTHROPIC_API_KEY` (or `GEMINI_API_KEY` with `AI_PROVIDER=gemini`).
- **Supabase** — `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` for the OB FAQ
  project (schema already applied there).

### 14.12 Security note (RLS)

The new `intv_*` tables (like the existing `interview_calls`) have **Row Level
Security disabled**. The pipeline uses the **service-role key**, which bypasses
RLS, so it works as-is — but the tables are exposed to the `anon`/`authenticated`
roles. If anything client-side uses the anon key, enable RLS and add policies:

```sql
ALTER TABLE public.intv_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_interviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_transcript_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intv_sync_logs ENABLE ROW LEVEL SECURITY;
-- then add policies appropriate to your access model (none = service-role only).
```

### DoD (analysis pipeline)

- [x] Google Sheet analyzed; columns/structure documented; messy layout handled.
- [x] Supabase schema (7 tables) designed, applied to OB FAQ, and verified
      (supersede trigger, score CHECK, FK cascade).
- [x] Status state machine (`new`…`analysis_done`/`error`).
- [x] Transcript source order Timeless → Google Docs → manual file; raw stored
      separately from cleaned.
- [x] Armenian-aware AI analysis returning strict JSON (Russian output) + scores
      + hire/maybe/reject/training recommendation with reasoning.
- [x] Idempotent, rerunnable, never crashes on missing link/empty transcript;
      every stage logged to `intv_sync_logs`.
- [x] Offline tests (`tests/test_interview_analysis_flow.py`, 17 cases).
