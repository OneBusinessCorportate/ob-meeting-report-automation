-- Task II — interview / onboarding call transcription.
--
-- Dedicated table that tracks each interview/onboarding call link, its
-- processing status, and the FULL transcript. Hiring decisions are made on
-- these calls, so the transcript is stored in full (never a summary).
--
-- Reuses the existing mtg_sources registry (the `timeless` source) so we don't
-- create a duplicate source system. Apply this once to the Supabase project:
--   psql "$SUPABASE_DB_URL" -f sql/interview_calls.sql
-- (or paste into the Supabase SQL editor).

CREATE TABLE IF NOT EXISTS public.interview_calls (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           uuid NOT NULL REFERENCES public.mtg_sources(id),
    source_call_id      text NOT NULL,          -- id extracted from the Timeless link
    call_url            text NOT NULL,          -- original interview/onboarding link
    candidate_name      text,
    role                text,                   -- e.g. 'бухгалтер'
    call_type           text NOT NULL DEFAULT 'interview',  -- interview | onboarding | training
    status              text NOT NULL DEFAULT 'pending',
        -- pending | transcript_found | saved
        -- | transcript_not_available | manual_action_required | failed
    decision            text,                   -- hiring decision, if captured later
    transcript_language text,
    raw_transcript      jsonb,                  -- full transcript: {type,language,source,text,segments}
    recording_url       text,
    duration_seconds    integer,
    error_message       text,
    metadata            jsonb,
    fetched_at          timestamptz,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    UNIQUE (source_id, source_call_id)
);

CREATE INDEX IF NOT EXISTS idx_interview_calls_status
    ON public.interview_calls (status);

-- Keep updated_at fresh (reuses the same trigger function as the mtg_ tables).
DROP TRIGGER IF EXISTS trg_interview_calls_updated ON public.interview_calls;
CREATE TRIGGER trg_interview_calls_updated
    BEFORE UPDATE ON public.interview_calls
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
