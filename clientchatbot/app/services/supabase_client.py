"""
Singleton Supabase client.
"""
from supabase import create_client, Client
from app.config import settings

_client: Client | None = None


def get_supabase() -> Client:
    """Returns the singleton Supabase client."""
    global _client
    if _client is None:
        if not settings.supabase_url or not settings.supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY are required.")
        _client = create_client(settings.supabase_url, settings.supabase_key)
    return _client
