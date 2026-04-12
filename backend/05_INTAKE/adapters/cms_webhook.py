"""
CMS webhook receiver: generic per-firm API key-authenticated endpoint.

Accepts POST requests with:
  - Documents as multipart file uploads
  - JSON metadata (case hint, document type, priority)
  - API key in X-API-Key header or ?api_key= query param

Endpoint: POST /api/intake/webhook/{firm_id}
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import NormalizedIntake

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
DOCS_DIR = os.path.join(BACKEND_DIR, "data_storage", "documents")


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def verify_api_key(firm_id: str, api_key: str) -> Optional[dict]:
    """Verify an API key against stored hashes. Returns the channel config or None."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    sb = _get_sb()

    resp = (
        sb.table("channel_api_keys")
        .select("id, channel_id, firm_id, expires_at, is_active")
        .eq("key_hash", key_hash)
        .eq("firm_id", firm_id)
        .eq("is_active", True)
        .execute()
    )

    if not resp.data:
        return None

    key_row = resp.data[0]

    # Check expiration
    if key_row.get("expires_at"):
        from datetime import datetime, timezone
        exp = datetime.fromisoformat(key_row["expires_at"].replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            return None

    # Fetch channel config
    ch_resp = (
        sb.table("connected_channels")
        .select("*")
        .eq("id", key_row["channel_id"])
        .eq("is_active", True)
        .execute()
    )
    if not ch_resp.data:
        return None

    return ch_resp.data[0]


def generate_api_key(firm_id: str, channel_id: str) -> str:
    """Generate a new API key for a CMS webhook channel. Returns the plaintext key."""
    sb = _get_sb()
    key = f"liq_{uuid.uuid4().hex}"  # liq_ prefix for easy identification
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    sb.table("channel_api_keys").insert({
        "channel_id": channel_id,
        "firm_id": firm_id,
        "key_hash": key_hash,
    }).execute()

    return key


def handle_cms_webhook(
    firm_id: str,
    api_key: str,
    files: list[tuple[str, bytes]],  # [(filename, content), ...]
    metadata: dict,
) -> dict:
    """Process an incoming CMS webhook with documents.

    Returns: {received, intake_ids, errors}
    """
    channel = verify_api_key(firm_id, api_key)
    if not channel:
        return {"error": "invalid_api_key", "received": 0}

    sb = _get_sb()
    intake_ids = []
    errors = []

    case_hint = metadata.get("case_id") or metadata.get("case_hint")
    priority = metadata.get("priority") or channel.get("default_priority", "soon")
    processing_mode = metadata.get("processing_mode", "balanced")

    for file_name, content in files:
        os.makedirs(DOCS_DIR, exist_ok=True)
        stem, ext = os.path.splitext(file_name)
        unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
        local_path = os.path.join(DOCS_DIR, unique_name)

        try:
            with open(local_path, "wb") as f:
                f.write(content)
        except Exception as e:
            errors.append({"file": file_name, "error": str(e)})
            continue

        file_hash = hashlib.sha256(content).hexdigest()

        # Dedup
        dup = sb.table("intake_queue").select("id").eq("file_hash", file_hash).eq("firm_id", firm_id).execute()
        if dup.data:
            continue

        row = {
            "firm_id": firm_id,
            "source_channel": "cms_webhook",
            "source_ref": metadata.get("external_id", ""),
            "source_metadata": {
                "channel_id": channel["id"],
                "cms_metadata": metadata,
            },
            "file_path": local_path,
            "file_name": unique_name,
            "file_hash": file_hash,
            "status": "pending",
            "process_priority": priority,
            "processing_mode": processing_mode,
        }
        if case_hint:
            row["explicit_case_hint"] = case_hint
        if channel.get("default_case_id"):
            row["target_case_id"] = channel["default_case_id"]
        if channel.get("default_corpus_id"):
            row["target_corpus_id"] = channel["default_corpus_id"]

        resp = sb.table("intake_queue").insert(row).execute()
        intake_ids.append(resp.data[0]["id"])

    return {
        "received": len(intake_ids),
        "intake_ids": intake_ids,
        "errors": errors,
    }
