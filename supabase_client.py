"""Shared Supabase service-role client for Python jobs."""

from __future__ import annotations

import os

from config import ConfigError


def create_supabase_client():  # type: ignore[no-untyped-def]
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ConfigError("Install supabase: pip install -r requirements.txt") from exc
    url = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise ConfigError(
            "SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL) and SUPABASE_SERVICE_ROLE_KEY are required."
        )
    return create_client(url, key)
