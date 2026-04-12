"""
Dropbox adapter: OAuth 2.0 + webhook verification + incremental sync.

Flow:
  1. User connects a Dropbox folder via OAuth → stored in connected_channels
  2. Dropbox sends GET /api/intake/dropbox/webhook?challenge=X for verification
  3. On file changes, Dropbox sends POST /api/intake/dropbox/webhook
  4. We use list_folder/continue with the stored cursor to get changed files
  5. Download new files, write to intake_queue

Can also run as a polling worker if webhooks aren't configured.
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

INGESTABLE_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".png", ".jpg", ".jpeg", ".tiff"}


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def _get_dbx(access_token: str):
    """Build a Dropbox client."""
    import dropbox
    return dropbox.Dropbox(access_token)


# ── Webhook handler ──────────────────────────────────────────────────────

def handle_dropbox_webhook_verify(challenge: str) -> str:
    """Dropbox webhook verification: echo back the challenge."""
    return challenge


def handle_dropbox_webhook(payload: dict) -> dict:
    """Process a Dropbox webhook notification.

    Dropbox sends: {"list_folder": {"accounts": ["dbid:AAA..."]}}
    We need to match the account to our connected channels and sync.
    """
    accounts = payload.get("list_folder", {}).get("accounts", [])
    if not accounts:
        return {"status": "ignored", "reason": "no accounts in payload"}

    sb = _get_sb()
    results = []

    for account_id in accounts:
        # Find channels linked to this Dropbox account
        resp = (
            sb.table("connected_channels")
            .select("*")
            .eq("channel_type", "dropbox")
            .eq("is_active", True)
            .execute()
        )
        for channel in (resp.data or []):
            config = channel.get("config", {})
            if config.get("dropbox_account_id") == account_id:
                results.append(_sync_dropbox_folder(channel))

    return {"status": "ok", "synced": len(results), "results": results}


# ── Polling sync ─────────────────────────────────────────────────────────

def poll_all_dropbox_channels() -> list[dict]:
    """Poll all active Dropbox channels for changes."""
    sb = _get_sb()
    resp = (
        sb.table("connected_channels")
        .select("*")
        .eq("channel_type", "dropbox")
        .eq("is_active", True)
        .execute()
    )
    results = []
    for channel in (resp.data or []):
        results.append(_sync_dropbox_folder(channel))
    return results


def _sync_dropbox_folder(channel: dict) -> dict:
    """Fetch new files from a Dropbox folder using cursor-based sync."""
    sb = _get_sb()
    config = channel.get("config", {})
    folder_path = config.get("folder_path", "")
    firm_id = channel["firm_id"]
    cursor = config.get("cursor")

    access_token = _resolve_token(config)
    if not access_token:
        return {"status": "error", "reason": "no access token"}

    try:
        dbx = _get_dbx(access_token)
    except Exception as e:
        return {"status": "error", "reason": f"Dropbox init failed: {e}"}

    try:
        if cursor:
            # Continue from where we left off
            result = dbx.files_list_folder_continue(cursor)
        else:
            # First sync — list entire folder
            result = dbx.files_list_folder(
                folder_path,
                recursive=True,
                include_media_info=False,
                include_deleted=False,
            )
    except Exception as e:
        return {"status": "error", "reason": f"Dropbox list failed: {e}"}

    intake_ids = []
    entries = result.entries

    # Handle pagination
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    import dropbox as dbx_module

    for entry in entries:
        if not isinstance(entry, dbx_module.files.FileMetadata):
            continue

        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in INGESTABLE_EXTENSIONS:
            continue

        file_hash = entry.content_hash  # Dropbox provides this

        # Dedup check
        if file_hash:
            dup = sb.table("intake_queue").select("id").eq("file_hash", file_hash).eq("firm_id", firm_id).execute()
            if dup.data:
                continue

        # Download file
        local_path = _download_dropbox_file(dbx, entry)
        if not local_path:
            continue

        if not file_hash:
            file_hash = _hash_file(local_path)

        row = {
            "firm_id": firm_id,
            "source_channel": "dropbox",
            "source_ref": entry.id,
            "source_metadata": {
                "path": entry.path_display,
                "folder_path": folder_path,
                "size": entry.size,
                "modified": entry.server_modified.isoformat() if entry.server_modified else None,
                "channel_id": channel["id"],
            },
            "file_path": local_path,
            "file_name": os.path.basename(local_path),
            "file_hash": file_hash,
            "status": "pending",
            "process_priority": channel.get("default_priority", "overnight"),
            "processing_mode": "balanced",
        }
        if channel.get("default_case_id"):
            row["target_case_id"] = channel["default_case_id"]
        if channel.get("default_corpus_id"):
            row["target_corpus_id"] = channel["default_corpus_id"]

        resp = sb.table("intake_queue").insert(row).execute()
        intake_ids.append(resp.data[0]["id"])

    # Update cursor for next sync
    config["cursor"] = result.cursor
    sb.table("connected_channels").update({"config": config}).eq("id", channel["id"]).execute()

    from datetime import datetime, timezone
    sb.table("connected_channels").update({
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", channel["id"]).execute()

    return {"status": "ok", "new_files": len(intake_ids), "intake_ids": intake_ids}


def _download_dropbox_file(dbx, entry) -> Optional[str]:
    """Download a file from Dropbox to local storage."""
    os.makedirs(DOCS_DIR, exist_ok=True)
    stem, ext = os.path.splitext(entry.name)
    local_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
    local_path = os.path.join(DOCS_DIR, local_name)

    try:
        _, response = dbx.files_download(entry.path_lower)
        with open(local_path, "wb") as f:
            f.write(response.content)
        return local_path
    except Exception as e:
        print(f"[dropbox] Download failed for {entry.name}: {e}", file=sys.stderr)
        return None


def _resolve_token(config: dict) -> Optional[str]:
    """Resolve Dropbox access token from config or environment."""
    token = os.environ.get("DROPBOX_ACCESS_TOKEN")
    if token:
        return token
    return config.get("oauth_credentials", {}).get("access_token")


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
