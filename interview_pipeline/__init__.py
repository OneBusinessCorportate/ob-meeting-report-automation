"""Task II — interview / onboarding call transcription automation.

Flow: interview call link (from the «Обучающий центр ОВ» table)
  → fetch FULL transcript via Timeless (link → transcript)
  → save the full transcript correctly & completely in Supabase
  → update per-link processing status.

Hiring decisions are made on these calls, so transcripts are stored in full
(never a summary). Reuses the shared Timeless client, config and Supabase
client from :mod:`meeting_pipeline`.
"""
