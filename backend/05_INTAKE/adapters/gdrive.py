"""
Google Drive adapter: OAuth 2.0 + watched folders + Drive changes API.

Flow:
  1. User connects a GDrive folder via OAuth → stored in connected_channels
  2. We register a Drive push notification (changes.watch) for that folder
  3. Google sends POST /api/intake/gdrive/webhook when files change
  4. We fetch the changed files, download new PDFs, write to intake_queue

Alternatively, this can run as a polling worker (every 5 min) if push
notifications aren't set up (e.g., behind NAT in dev).

OAuth tokens are stored encrypted in connected_channels.config.oauth_token_ref
which points to a credential stored via the auth system (or env var for dev).
"""
from __future__ import annotations

import hashlib
import io
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

# Supported MIME types for ingestion
INGESTABLE_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/tiff",
}

# Google Docs export to PDF
GOOGLE_DOC_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
}


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def _get_drive_service(credentials_json: dict):
    """Build a Google Drive API v3 service from OAuth credentials."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=credentials_json.get("access_token"),
        refresh_token=credentials_json.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    )
    return build("drive", "v3", credentials=creds)


# ── Webhook handler (called by POST /api/intake/gdrive/webhook) ──────────

def handle_gdrive_webhook(headers: dict, body: bytes) -> dict:
    """Process a Google Drive push notification.

    Google sends:
      X-Goog-Channel-ID: <our channel_id from watch registration>
      X-Goog-Resource-State: change | sync | ...
    """
    channel_id = headers.get("x-goog-channel-id", "")
    resource_state = headers.get("x-goog-resource-state", "")

    if resource_state == "sync":
        return {"status": "sync_ack"}

    if resource_state != "change":
        return {"status": "ignored", "reason": f"resource_state={resource_state}"}

    # Look up the connected channel by watch_channel_id
    sb = _get_sb()
    resp = sb.table("connected_channels").select("*").eq(
        "config->>watch_channel_id", channel_id
    ).eq("is_active", True).execute()

    if not resp.data:
        return {"status": "ignored", "reason": "unknown channel_id"}

    channel = resp.data[0]
    return _sync_folder(channel)


# ── Polling sync (alternative to webhooks) ───────────────────────────────

def poll_all_gdrive_channels() -> list[dict]:
    """Poll all active GDrive channels for changes. Run on a timer."""
    sb = _get_sb()
    resp = (
        sb.table("connected_channels")
        .select("*")
        .eq("channel_type", "gdrive")
        .eq("is_active", True)
        .execute()
    )
    results = []
    for channel in (resp.data or []):
        results.append(_sync_folder(channel))
    return results


def _sync_folder(channel: dict) -> dict:
    """Fetch new files from a Drive folder and write to intake_queue."""
    sb = _get_sb()
    config = channel.get("config", {})
    folder_id = config.get("folder_id")
    firm_id = channel["firm_id"]

    if not folder_id:
        return {"status": "error", "reason": "no folder_id in config"}

    # Get OAuth credentials
    creds = _resolve_credentials(config)
    if not creds:
        return {"status": "error", "reason": "could not resolve OAuth credentials"}

    try:
        service = _get_drive_service(creds)
    except Exception as e:
        return {"status": "error", "reason": f"Drive API init failed: {e}"}

    # List files in folder, modified since last sync
    query = f"'{folder_id}' in parents and trashed = false"
    last_sync = channel.get("last_sync_at")
    if last_sync:
        query += f" and modifiedTime > '{last_sync}'"

    try:
        results = service.files().list(
            q=query,
            fields="files(id, name, mimeType, size, modifiedTime, md5Checksum)",
            pageSize=100,
            orderBy="modifiedTime",
        ).execute()
    except Exception as e:
        return {"status": "error", "reason": f"Drive list failed: {e}"}

    files = results.get("files", [])
    if not files:
        _update_last_sync(sb, channel["id"])
        return {"status": "ok", "new_files": 0}

    intake_ids = []
    for f in files:
        mime = f.get("mimeType", "")
        if mime not in INGESTABLE_MIMES and mime not in GOOGLE_DOC_MIMES:
            continue

        file_name = f["name"]
        drive_file_id = f["id"]

        # Download the file
        local_path = _download_drive_file(service, drive_file_id, file_name, mime)
        if not local_path:
            continue

        file_hash = _hash_file(local_path)

        # Dedup check
        dup = sb.table("intake_queue").select("id").eq("file_hash", file_hash).eq("firm_id", firm_id).execute()
        if dup.data:
            continue

        intake = NormalizedIntake(
            firm_id=uuid.UUID(firm_id),
            file_name=os.path.basename(local_path),
            file_path=local_path,
            source_channel="gdrive",
            source_ref=drive_file_id,
            source_metadata={
                "folder_id": folder_id,
                "folder_name": config.get("folder_name", ""),
                "mime_type": mime,
                "modified_time": f.get("modifiedTime"),
                "channel_id": channel["id"],
            },
            process_priority=channel.get("default_priority", "overnight"),
            processing_mode="balanced",
            file_hash=file_hash,
        )

        # Check if channel has a default case
        explicit_hint = None
        if channel.get("default_case_id"):
            explicit_hint = channel["default_case_id"]
            intake.explicit_case_hint = explicit_hint

        row = {
            "firm_id": firm_id,
            "source_channel": "gdrive",
            "source_ref": drive_file_id,
            "source_metadata": intake.source_metadata,
            "file_path": local_path,
            "file_name": intake.file_name,
            "file_hash": file_hash,
            "status": "pending",
            "process_priority": intake.process_priority,
            "processing_mode": intake.processing_mode,
        }
        if explicit_hint:
            row["explicit_case_hint"] = explicit_hint
        if channel.get("default_case_id"):
            row["target_case_id"] = channel["default_case_id"]
        if channel.get("default_corpus_id"):
            row["target_corpus_id"] = channel["default_corpus_id"]

        resp = sb.table("intake_queue").insert(row).execute()
        intake_ids.append(resp.data[0]["id"])

    _update_last_sync(sb, channel["id"])
    return {"status": "ok", "new_files": len(intake_ids), "intake_ids": intake_ids}


def _download_drive_file(service, file_id: str, name: str, mime: str) -> Optional[str]:
    """Download a file from Drive to local storage. Returns local path."""
    from googleapiclient.http import MediaIoBaseDownload

    os.makedirs(DOCS_DIR, exist_ok=True)
    stem, ext = os.path.splitext(name)
    local_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext or '.pdf'}"
    local_path = os.path.join(DOCS_DIR, local_name)

    try:
        if mime in GOOGLE_DOC_MIMES:
            # Export Google Docs as PDF
            request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
            local_path = os.path.join(DOCS_DIR, f"{stem}_{uuid.uuid4().hex[:8]}.pdf")
        else:
            request = service.files().get_media(fileId=file_id)

        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        return local_path
    except Exception as e:
        print(f"[gdrive] Download failed for {name}: {e}", file=sys.stderr)
        return None


def _resolve_credentials(config: dict) -> Optional[dict]:
    """Resolve OAuth credentials from config or environment."""
    # For dev: use env vars directly
    access_token = os.environ.get("GDRIVE_ACCESS_TOKEN")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    if access_token:
        return {"access_token": access_token, "refresh_token": refresh_token}

    # For prod: look up from config.oauth_token_ref
    token_ref = config.get("oauth_token_ref")
    if token_ref:
        # This would look up from a secure token store
        return config.get("oauth_credentials")

    return None


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_last_sync(sb, channel_id: str):
    from datetime import datetime, timezone
    sb.table("connected_channels").update({
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", channel_id).execute()


# ── Watch registration (called once per folder setup) ────────────────────

def register_drive_watch(channel_id: str, webhook_url: str) -> dict:
    """Register a push notification channel for a connected Drive folder.

    Call this after the user connects a folder in the settings UI.
    """
    sb = _get_sb()
    resp = sb.table("connected_channels").select("*").eq("id", channel_id).execute()
    if not resp.data:
        return {"error": "channel not found"}

    channel = resp.data[0]
    config = channel.get("config", {})
    creds = _resolve_credentials(config)
    if not creds:
        return {"error": "no credentials"}

    service = _get_drive_service(creds)
    watch_id = str(uuid.uuid4())

    try:
        result = service.files().watch(
            fileId=config["folder_id"],
            body={
                "id": watch_id,
                "type": "web_hook",
                "address": webhook_url,
            },
        ).execute()

        # Store watch info in config
        config["watch_channel_id"] = watch_id
        config["watch_resource_id"] = result.get("resourceId")
        config["watch_expiration"] = result.get("expiration")

        sb.table("connected_channels").update({
            "config": config,
        }).eq("id", channel_id).execute()

        return {"status": "watching", "watch_id": watch_id, "expiration": result.get("expiration")}
    except Exception as e:
        return {"error": str(e)}
