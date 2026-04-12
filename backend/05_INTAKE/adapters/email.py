"""
Email inbound adapter: parses Postmark/SendGrid inbound parse webhooks
and normalizes them into NormalizedIntake items for the intake queue.

Expected webhook formats:
  - Postmark: JSON with To, From, Subject, Attachments[]
  - SendGrid: multipart/form-data with to, from, subject, attachment-info

Per-firm email addresses:
  - firm{id}@ingest.yourapp.com  (firm-level, routes via filename/LLM)
  - firm{id}+case-{slug}@ingest.yourapp.com  (case-specific, skips routing)
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import NormalizedIntake


# ── Address parsing ──────────────────────────────────────────────────────

# Matches: firm<uuid>@domain or firm<uuid>+case-<slug>@domain
FIRM_ADDRESS_RE = re.compile(
    r"firm([0-9a-f\-]{36})(?:\+case-([^\s@]+))?@",
    re.IGNORECASE,
)


def extract_firm_and_case_hint(to_address: str) -> tuple[Optional[str], Optional[str]]:
    """Extract firm_id and optional case hint from a plus-addressed email.

    Returns (firm_id_str, case_hint_or_none).
    """
    match = FIRM_ADDRESS_RE.search(to_address)
    if not match:
        return None, None
    firm_id = match.group(1)
    case_hint = match.group(2)  # e.g., "epic-v-apple" or a UUID
    return firm_id, case_hint


# ── Postmark format ──────────────────────────────────────────────────────

def parse_postmark_inbound(payload: dict) -> list[NormalizedIntake]:
    """Parse a Postmark inbound webhook payload.

    Returns one NormalizedIntake per attachment.
    """
    to_addr  = payload.get("To") or payload.get("ToFull", [{}])[0].get("Email", "")
    from_addr = payload.get("From") or payload.get("FromFull", {}).get("Email", "")
    subject  = payload.get("Subject", "")
    attachments = payload.get("Attachments", [])

    firm_id_str, case_hint = extract_firm_and_case_hint(to_addr)
    if not firm_id_str:
        # Could not determine firm from address — reject
        return []

    if not attachments:
        # No attachments — nothing to ingest
        return []

    results = []
    for att in attachments:
        name = att.get("Name", "unknown.pdf")
        content = att.get("Content", "")  # base64-encoded
        content_type = att.get("ContentType", "")

        # Skip non-document attachments
        if content_type and not any(
            t in content_type for t in ["pdf", "doc", "text", "image", "octet-stream"]
        ):
            continue

        file_hash = hashlib.sha256(content.encode()).hexdigest() if content else None

        results.append(NormalizedIntake(
            firm_id=uuid.UUID(firm_id_str),
            file_name=name,
            file_path="",  # will be set after saving to storage
            source_channel="email",
            source_ref=payload.get("MessageID", ""),
            source_metadata={
                "sender": from_addr,
                "subject": subject,
                "to": to_addr,
                "content_type": content_type,
            },
            explicit_case_hint=case_hint,
            process_priority="overnight",  # default for email
            processing_mode="balanced",
            file_hash=file_hash,
        ))

    return results


# ── SendGrid format ──────────────────────────────────────────────────────

def parse_sendgrid_inbound(form_data: dict) -> list[NormalizedIntake]:
    """Parse a SendGrid inbound parse webhook (form data).

    Returns one NormalizedIntake per attachment.
    """
    import json

    to_addr  = form_data.get("to", "")
    from_addr = form_data.get("from", "")
    subject  = form_data.get("subject", "")

    firm_id_str, case_hint = extract_firm_and_case_hint(to_addr)
    if not firm_id_str:
        return []

    # Parse attachment info
    attachment_info = form_data.get("attachment-info", "{}")
    try:
        att_info = json.loads(attachment_info) if isinstance(attachment_info, str) else attachment_info
    except json.JSONDecodeError:
        att_info = {}

    if not att_info:
        return []

    results = []
    for key, info in att_info.items():
        name = info.get("filename", info.get("name", "unknown.pdf"))
        content_type = info.get("type", info.get("content-type", ""))

        if content_type and not any(
            t in content_type for t in ["pdf", "doc", "text", "image", "octet-stream"]
        ):
            continue

        results.append(NormalizedIntake(
            firm_id=uuid.UUID(firm_id_str),
            file_name=name,
            file_path="",
            source_channel="email",
            source_ref=form_data.get("Message-Id", ""),
            source_metadata={
                "sender": from_addr,
                "subject": subject,
                "to": to_addr,
                "content_type": content_type,
                "attachment_key": key,
            },
            explicit_case_hint=case_hint,
            process_priority="overnight",
            processing_mode="balanced",
        ))

    return results
