"""Google service-account credential helpers (Sheets / Docs / Drive).

All Google deps are imported lazily so the pipeline (and the offline tests) work
without them installed. Credentials come from either an inline JSON blob
(GOOGLE_SERVICE_ACCOUNT_JSON) or a path to the key file
(GOOGLE_SERVICE_ACCOUNT_FILE) — never hard-coded.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.utils import get_logger

log = get_logger("interview_pipeline.google_creds")

# Read-only scopes are enough to read the sheet and export Google Docs.
READONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
# Write scope, only needed for the optional sheet write-back feature.
SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def has_google_credentials(config: Config) -> bool:
    return bool(config.google_service_account_json or config.google_service_account_file)


def load_service_account_info(config: Config) -> Dict[str, Any]:
    """Return the service-account key as a dict (from blob or file)."""
    if config.google_service_account_json:
        return json.loads(config.google_service_account_json)
    if config.google_service_account_file:
        with open(config.google_service_account_file, "r", encoding="utf-8") as fh:
            return json.load(fh)
    raise RuntimeError(
        "Google credentials not configured: set GOOGLE_SERVICE_ACCOUNT_JSON or "
        "GOOGLE_SERVICE_ACCOUNT_FILE."
    )


def build_credentials(config: Config, scopes: Optional[List[str]] = None):
    from google.oauth2 import service_account  # lazy

    info = load_service_account_info(config)
    return service_account.Credentials.from_service_account_info(
        info, scopes=scopes or READONLY_SCOPES
    )


def build_gspread_client(config: Config):
    import gspread  # lazy

    return gspread.authorize(build_credentials(config, READONLY_SCOPES))


def authorized_session(config: Config, scopes: Optional[List[str]] = None):
    """An ``AuthorizedSession`` (requests) for raw Google API/file calls."""
    from google.auth.transport.requests import AuthorizedSession  # lazy

    return AuthorizedSession(build_credentials(config, scopes or READONLY_SCOPES))
