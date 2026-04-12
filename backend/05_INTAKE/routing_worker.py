"""
Routing worker: determines which case/corpus an intake item belongs to.

Cascade (cheap -> expensive):
  1. Explicit metadata (plus-address hint, API header, UI pre-fill)
  2. Filename heuristics (case number regex, party name match)
  3. LLM classification (Gemini Flash, first-page text only)
  4. Fall through -> awaiting_confirmation (user decides)
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from models import RoutingResult


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


# ── Stage 1: Explicit metadata ──────────────────────────────────────────

def _try_metadata_routing(
    source_metadata: dict,
    explicit_case_hint: Optional[str],
    firm_id: uuid.UUID,
) -> Optional[RoutingResult]:
    sb = _get_sb()

    hint = explicit_case_hint or source_metadata.get("case_hint")
    if not hint:
        return None

    # Try as UUID
    try:
        case_uuid = uuid.UUID(hint)
        resp = (
            sb.table("cases").select("id, case_name")
            .eq("id", str(case_uuid))
            .eq("firm_id", str(firm_id))
            .execute()
        )
        if resp.data:
            return RoutingResult(
                suggested_case_id=uuid.UUID(resp.data[0]["id"]),
                confidence=0.99,
                method="metadata",
                reasoning=f"Explicit UUID hint matched: {resp.data[0]['case_name']}",
            )
    except ValueError:
        pass

    # Try as name substring
    resp = (
        sb.table("cases").select("id, case_name")
        .eq("firm_id", str(firm_id))
        .ilike("case_name", f"%{hint}%")
        .execute()
    )
    matches = resp.data or []
    if len(matches) == 1:
        return RoutingResult(
            suggested_case_id=uuid.UUID(matches[0]["id"]),
            confidence=0.95,
            method="metadata",
            reasoning=f"Case hint '{hint}' matched: {matches[0]['case_name']}",
        )
    elif len(matches) > 1:
        return RoutingResult(
            confidence=0.5,
            method="metadata",
            reasoning=f"Case hint '{hint}' matched {len(matches)} cases",
            candidates=[{"id": m["id"], "name": m["case_name"]} for m in matches[:5]],
        )
    return None


# ── Stage 2: Filename heuristics ─────────────────────────────────────────

CASE_NUMBER_RE = re.compile(
    r"(?:(?:No|Case|Docket)[.\s#:]*)?(\d{2,4}[-\s]?(?:cv|civ|cr|ap|mc)[-\s]?\d{3,6})",
    re.IGNORECASE,
)


def _try_filename_routing(file_name: str, firm_id: uuid.UUID) -> Optional[RoutingResult]:
    sb = _get_sb()

    match = CASE_NUMBER_RE.search(file_name)
    if match:
        case_num = match.group(1).replace(" ", "-").upper()
        resp = (
            sb.table("cases").select("id, case_name")
            .eq("firm_id", str(firm_id))
            .ilike("case_name", f"%{case_num}%")
            .execute()
        )
        if resp.data and len(resp.data) == 1:
            return RoutingResult(
                suggested_case_id=uuid.UUID(resp.data[0]["id"]),
                confidence=0.88,
                method="filename",
                reasoning=f"Case number '{case_num}' in filename matched: {resp.data[0]['case_name']}",
            )

    # Party name matching
    stem = os.path.splitext(file_name)[0]
    stem = re.sub(r"_[0-9a-f]{8}$", "", stem)

    if len(stem) > 3:
        resp = sb.table("cases").select("id, case_name").eq("firm_id", str(firm_id)).execute()
        for case in (resp.data or []):
            words = [w for w in re.split(r"[\s_\-]+", stem) if len(w) > 3]
            case_lower = case["case_name"].lower()
            matching = [w for w in words if w.lower() in case_lower]
            if len(matching) >= 2:
                return RoutingResult(
                    suggested_case_id=uuid.UUID(case["id"]),
                    confidence=0.75,
                    method="filename",
                    reasoning=f"Words {matching} matched case: {case['case_name']}",
                )
    return None


# ── Stage 3: LLM classification ─────────────────────────────────────────

async def _try_llm_routing(
    file_name: str,
    first_page_text: str,
    source_metadata: dict,
    firm_id: uuid.UUID,
) -> Optional[RoutingResult]:
    import google.generativeai as genai

    sb = _get_sb()
    resp = (
        sb.table("cases")
        .select("id, case_name, case_stage, case_context")
        .eq("firm_id", str(firm_id))
        .execute()
    )
    cases = resp.data or []
    if not cases:
        return RoutingResult(confidence=0.0, method="llm", reasoning="Firm has no cases")

    cases_list = "\n".join(
        f"- ID: {c['id']} | Name: {c['case_name']} | Stage: {c.get('case_stage', 'unknown')}"
        for c in cases
    )

    prompt = f"""You are a legal document routing assistant. Given the document info and first page, determine which case it belongs to.

DOCUMENT:
  Filename: {file_name}
  Source: {source_metadata.get('source_channel', 'unknown')}
  Sender: {source_metadata.get('sender', 'unknown')}

FIRST PAGE (truncated):
{first_page_text[:2000]}

AVAILABLE CASES:
{cases_list}

Respond with ONLY JSON: {{"case_id": "<uuid or null>", "confidence": <0.0-1.0>, "reasoning": "<brief>"}}"""

    genai.configure(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    model = genai.GenerativeModel("gemini-2.0-flash")

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)

        case_id = result.get("case_id")
        conf = float(result.get("confidence", 0))

        if case_id and case_id != "null":
            return RoutingResult(
                suggested_case_id=uuid.UUID(case_id),
                confidence=conf,
                method="llm",
                reasoning=result.get("reasoning", "LLM classification"),
            )
        return RoutingResult(confidence=conf, method="llm", reasoning=result.get("reasoning", ""))
    except Exception as e:
        print(f"[routing] LLM classification failed: {e}", file=sys.stderr)
        return None


# ── Main routing function ────────────────────────────────────────────────

async def route_intake_item(
    intake_id: uuid.UUID,
    firm_id: uuid.UUID,
    file_name: str,
    source_channel: str,
    source_metadata: dict,
    explicit_case_hint: str | None = None,
    first_page_text: str = "",
    *,
    confidence_threshold: float = 0.85,
) -> RoutingResult:
    sb = _get_sb()
    sb.table("intake_queue").update({"status": "routing"}).eq("id", str(intake_id)).execute()

    # Stage 1
    r1 = _try_metadata_routing(source_metadata, explicit_case_hint, firm_id)
    if r1 and r1.confidence >= confidence_threshold:
        _apply_routing(sb, intake_id, r1, auto_confirm=True)
        return r1

    # Stage 2
    r2 = _try_filename_routing(file_name, firm_id)
    if r2 and r2.confidence >= confidence_threshold:
        _apply_routing(sb, intake_id, r2, auto_confirm=True)
        return r2

    # Stage 3
    r3 = await _try_llm_routing(file_name, first_page_text, source_metadata, firm_id)
    if r3 and r3.confidence >= confidence_threshold:
        _apply_routing(sb, intake_id, r3, auto_confirm=True)
        return r3

    # Best effort
    best = max(
        filter(None, [r1, r2, r3]),
        key=lambda r: r.confidence,
        default=RoutingResult(method="unresolved", reasoning="All routing stages failed"),
    )
    _apply_routing(sb, intake_id, best, auto_confirm=False)
    return best


def _apply_routing(sb, intake_id: uuid.UUID, result: RoutingResult, auto_confirm: bool):
    update: dict = {"routing_result": result.to_json()}
    if auto_confirm:
        update["status"] = "confirmed"
        if result.suggested_case_id:
            update["target_case_id"] = str(result.suggested_case_id)
    else:
        update["status"] = "awaiting_confirmation"
    sb.table("intake_queue").update(update).eq("id", str(intake_id)).execute()
