"""Supabase client — single shared instance for all backend Supabase operations.

Uses the service role key so the backend can read/write any user's data
(RLS is enforced on the frontend JS client, not here).
The service role key never leaves the server.
"""

import os
import logging
from supabase import create_client, Client

log = logging.getLogger(__name__)

_client: Client | None = None


def get_supabase() -> Client:
    """Return the shared Supabase service-role client, creating it on first call."""
    global _client
    if _client is None:
        from dotenv import load_dotenv
        import pathlib
        load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

        url = (os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL", "")).strip().rstrip("/")
        key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_SERVICE_ROLE_KEY", "")).strip()
        if not url or not key:
            raise RuntimeError(
                "VITE_SUPABASE_URL and VITE_SUPABASE_SERVICE_ROLE_KEY must be set in environment"
            )
        _client = create_client(url, key)
        log.info("Supabase client initialized for %s", url)
    return _client
