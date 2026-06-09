-- =============================================================================
-- Training Center / Interview Analysis — normalized schema
-- (project: «Обучающий центр / анализ собеседований»)
--
-- Pipeline: Google Sheet «Обучающий центр ОВ» (source)
--   → resolve FULL transcript (Timeless API → Google Docs → manual file)
--   → store raw + cleaned transcript (+ segments)
--   → AI analysis of the interview (Armenian-aware, Russian output)
--   → scores + hire/maybe/reject/training recommendation
--   → processing status per interview, full run logging.
--
-- Tables (requested name → implemented, namespaced `intv_` to live safely in the
-- shared OB FAQ database next to the existing kb_/mtg_/rag_ tables):
--   candidates           → intv_candidates
--   interviews           → intv_interviews
--   transcripts          → intv_transcripts
--   transcript_segments  → intv_transcript_segments
--   interview_analysis   → intv_analyses
--   interview_scores     → intv_scores
--   sync_logs            → intv_sync_logs
--
-- Reuses the existing public.mtg_sources registry (the `timeless` source) for the
-- transcript source FK, so we do not create a parallel source system.
--
-- Apply once (idempotent):
--   psql "$SUPABASE_DB_URL" -f sql/interview_analysis_schema.sql
--   -- or paste into the Supabase SQL editor / apply via MCP.
-- =============================================================================

-- Shared trigger to keep updated_at fresh. CREATE OR REPLACE is idempotent and
-- matches the function already used by the mtg_* / interview_calls tables.
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 1. Candidates  (one person; deduped per hiring track)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_candidates (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name          text NOT NULL,
    normalized_name    text NOT NULL,                 -- lower/trimmed, for dedup
    track              text NOT NULL DEFAULT 'buh',   -- buh | consultant_buh | jurist | other
    role               text,                          -- e.g. 'бухгалтер'
    email              text,
    phone              text,
    contact_raw        text,                          -- original contact cell
    resume_comment     text,
    -- Two free-text status columns from the sheet (both named «Статус»):
    sheet_status       text,   -- test/process status ("тест заполнен", "тест отправлен", …)
    decision_status    text,   -- final hiring decision ("оффер отправлен", "мы отказали", "не подходит", …)
    grade_start        text,
    test_score         numeric,
    test_sent_at       date,
    probation_start    date,
    terminated_at      date,
    termination_reason text,
    -- Source provenance so we can trace every row back to the spreadsheet:
    source_sheet       text,
    source_row         integer,
    metadata           jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- Same person can exist in different tracks (Бух / Консультант / Юрист).
    UNIQUE (track, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_intv_candidates_track  ON public.intv_candidates (track);
CREATE INDEX IF NOT EXISTS idx_intv_candidates_status ON public.intv_candidates (sheet_status);

DROP TRIGGER IF EXISTS trg_intv_candidates_updated ON public.intv_candidates;
CREATE TRIGGER trg_intv_candidates_updated
    BEFORE UPDATE ON public.intv_candidates
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

-- -----------------------------------------------------------------------------
-- 2. Interviews  (one interview / onboarding / training call)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_interviews (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id      uuid NOT NULL REFERENCES public.intv_candidates(id) ON DELETE CASCADE,
    source_id         uuid REFERENCES public.mtg_sources(id),  -- the `timeless` source
    interview_type    text NOT NULL DEFAULT 'interview',       -- interview | onboarding | training
    call_url          text,                                    -- link from the sheet (may be null)
    -- Where the transcript actually came from: timeless | google_docs | manual_file | none
    transcript_source text,
    -- Stable id for dedup: Timeless meeting id, Google-Doc id, or a hash of the url.
    source_call_id    text NOT NULL,
    title             text,
    language          text,                                    -- hy (Armenian) by default
    recording_url     text,
    duration_seconds  integer,
    held_at           timestamptz,
    -- Processing status state machine:
    --   new | link_missing | transcript_pending | transcript_ready
    --   | analysis_pending | analysis_done | error
    status            text NOT NULL DEFAULT 'new',
    error_message     text,
    sheet_ref         jsonb,                                   -- {sheet,row,column}
    metadata          jsonb,
    fetched_at        timestamptz,                             -- when transcript was fetched
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- Dedup: one interview per (source, stable call id). Safe to rerun.
    UNIQUE (source_id, source_call_id)
);
CREATE INDEX IF NOT EXISTS idx_intv_interviews_candidate ON public.intv_interviews (candidate_id);
CREATE INDEX IF NOT EXISTS idx_intv_interviews_status    ON public.intv_interviews (status);

DROP TRIGGER IF EXISTS trg_intv_interviews_updated ON public.intv_interviews;
CREATE TRIGGER trg_intv_interviews_updated
    BEFORE UPDATE ON public.intv_interviews
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

-- -----------------------------------------------------------------------------
-- 3. Transcripts  (raw kept SEPARATE from processed/cleaned; one per interview)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_transcripts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id  uuid NOT NULL REFERENCES public.intv_interviews(id) ON DELETE CASCADE,
    language      text,
    source        text,                       -- timeless | google_docs | manual_file
    raw_text      text NOT NULL,              -- exactly what the source returned (raw layer)
    cleaned_text  text,                       -- normalized text used for analysis (processed)
    raw_payload   jsonb,                      -- original API/document payload, for re-processing
    char_count    integer,
    word_count    integer,
    segment_count integer,
    speaker_count integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (interview_id)                      -- one current transcript per interview (upserted)
);
CREATE INDEX IF NOT EXISTS idx_intv_transcripts_interview ON public.intv_transcripts (interview_id);

DROP TRIGGER IF EXISTS trg_intv_transcripts_updated ON public.intv_transcripts;
CREATE TRIGGER trg_intv_transcripts_updated
    BEFORE UPDATE ON public.intv_transcripts
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

-- -----------------------------------------------------------------------------
-- 4. Transcript segments  (diarized lines; optional, populated when available)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_transcript_segments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transcript_id uuid NOT NULL REFERENCES public.intv_transcripts(id) ON DELETE CASCADE,
    interview_id  uuid NOT NULL REFERENCES public.intv_interviews(id) ON DELETE CASCADE,
    idx           integer NOT NULL,           -- order within the transcript
    speaker       text,
    start_ms      integer,
    end_ms        integer,
    text          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transcript_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_intv_segments_transcript ON public.intv_transcript_segments (transcript_id);

-- -----------------------------------------------------------------------------
-- 5. Interview analysis  (AI output; versioned, is_current marks the latest)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_analyses (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id        uuid NOT NULL REFERENCES public.intv_interviews(id) ON DELETE CASCADE,
    candidate_id        uuid REFERENCES public.intv_candidates(id) ON DELETE CASCADE,
    version             integer NOT NULL DEFAULT 1,
    is_current          boolean NOT NULL DEFAULT true,
    status              text NOT NULL DEFAULT 'completed',     -- completed | failed | superseded
    model_id            text,
    prompt_version      text,
    transcript_language text,                                  -- detected/declared language
    summary             text,                                  -- Russian summary
    summary_original    text,                                  -- summary in original language (optional)
    candidate_strengths jsonb,                                 -- [string]
    candidate_weaknesses jsonb,                                -- [string]
    red_flags           jsonb,                                 -- [string]
    next_steps          jsonb,                                 -- [string]
    recommendation      text,                                  -- hire | maybe | reject | training
    reasoning           text,
    ai_metadata         jsonb,                                 -- {usage, raw_text, …}
    processing_time_ms  integer,
    error_message       text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intv_analyses_interview ON public.intv_analyses (interview_id);
CREATE INDEX IF NOT EXISTS idx_intv_analyses_candidate ON public.intv_analyses (candidate_id);
-- At most one current analysis per interview.
CREATE UNIQUE INDEX IF NOT EXISTS uq_intv_analyses_current
    ON public.intv_analyses (interview_id) WHERE is_current;

DROP TRIGGER IF EXISTS trg_intv_analyses_updated ON public.intv_analyses;
CREATE TRIGGER trg_intv_analyses_updated
    BEFORE UPDATE ON public.intv_analyses
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

-- When a new is_current analysis is inserted, demote the previous current one.
-- Runs BEFORE INSERT so the partial unique index (one is_current per interview)
-- is never momentarily violated. Column defaults (incl. id) are applied before
-- BEFORE triggers fire, so NEW.id is already populated here.
CREATE OR REPLACE FUNCTION public.intv_supersede_old_analyses()
RETURNS trigger AS $$
BEGIN
    IF NEW.is_current THEN
        UPDATE public.intv_analyses
           SET is_current = false,
               status = CASE WHEN status = 'completed' THEN 'superseded' ELSE status END
         WHERE interview_id = NEW.interview_id
           AND id <> NEW.id
           AND is_current;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_intv_supersede_old_analyses ON public.intv_analyses;
CREATE TRIGGER trg_intv_supersede_old_analyses
    BEFORE INSERT ON public.intv_analyses
    FOR EACH ROW EXECUTE FUNCTION public.intv_supersede_old_analyses();

-- -----------------------------------------------------------------------------
-- 6. Interview scores  (numeric scores, kept separate; 1:1 with an analysis row)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_scores (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id         uuid NOT NULL REFERENCES public.intv_analyses(id) ON DELETE CASCADE,
    interview_id        uuid NOT NULL REFERENCES public.intv_interviews(id) ON DELETE CASCADE,
    candidate_id        uuid REFERENCES public.intv_candidates(id) ON DELETE CASCADE,
    score_scale         text NOT NULL DEFAULT '0-10',
    communication_score smallint,
    professional_score  smallint,
    motivation_score    smallint,
    overall_score       smallint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (analysis_id),
    CONSTRAINT chk_intv_scores_range CHECK (
        (communication_score IS NULL OR communication_score BETWEEN 0 AND 10) AND
        (professional_score  IS NULL OR professional_score  BETWEEN 0 AND 10) AND
        (motivation_score    IS NULL OR motivation_score    BETWEEN 0 AND 10) AND
        (overall_score       IS NULL OR overall_score       BETWEEN 0 AND 10)
    )
);
CREATE INDEX IF NOT EXISTS idx_intv_scores_interview ON public.intv_scores (interview_id);

-- -----------------------------------------------------------------------------
-- 7. Sync / processing logs  (one row per stage event; groups runs by run_id)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.intv_sync_logs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       uuid NOT NULL,
    stage        text NOT NULL,              -- run | sheet_read | upsert | transcript | analysis | deliver | writeback
    level        text NOT NULL DEFAULT 'info', -- info | warning | error
    status       text,                       -- per-stage outcome / interview status
    interview_id uuid REFERENCES public.intv_interviews(id) ON DELETE SET NULL,
    candidate_id uuid REFERENCES public.intv_candidates(id) ON DELETE SET NULL,
    message      text,
    detail       jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intv_sync_logs_run   ON public.intv_sync_logs (run_id);
CREATE INDEX IF NOT EXISTS idx_intv_sync_logs_level ON public.intv_sync_logs (level);
