"""OneBusiness meeting report automation pipeline.

Flow: Timeless / full transcript -> Supabase L1 (mtg_meetings.raw_transcript)
-> AI analysis -> Supabase L2 (mtg_analyses) -> Telegram markdown report.
"""

__version__ = "1.0.0"
