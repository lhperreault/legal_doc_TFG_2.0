"""
document_summary.py — Generate and persist a professional case summary on document upload.

Runs automatically when a new document is ingested (triggered by server.py / run.py).
Uses the general_agent to produce a structured markdown summary covering:
  - Case overview & parties
  - Document inventory
  - Key claims / obligations
  - Relief sought / key terms
  - Critical dates & key facts
  - Strategic considerations

Persists the result to:
  1. agent_responses (session_id = "document_summary") — queryable by the frontend
  2. cases.ai_summary column — direct case-level access for the legal pad UI

Required Supabase migration (run once in the SQL editor):

    ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS ai_summary               TEXT,
        ADD COLUMN IF NOT EXISTS ai_summary_confidence    FLOAT,
        ADD COLUMN IF NOT EXISTS ai_summary_generated_at  TIMESTAMPTZ;

    CREATE INDEX IF NOT EXISTS idx_agent_responses_doc_summary
        ON agent_responses (case_id, session_id)
        WHERE session_id = 'document_summary';

Usage:
    python backend/04_AGENTIC_ARCHITECTURE/document_summary.py --case_id "uuid"
"""

import argparse
import os
import sys
import uuid

_ARCH_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_ARCH_DIR, "..", ".."))

if _ARCH_DIR not in sys.path:
    sys.path.insert(0, _ARCH_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Load graph via importlib (avoids digit-prefixed package issues)
import importlib.util as _ilu
_graph_spec = _ilu.spec_from_file_location(
    "graph_module", os.path.join(_ARCH_DIR, "graph.py")
)
_graph_mod = _ilu.module_from_spec(_graph_spec)
_graph_spec.loader.exec_module(_graph_mod)
build_graph = _graph_mod.build_graph

from langchain_core.messages import HumanMessage


# ── Summary prompt ─────────────────────────────────────────────────────────────

_SUMMARY_QUERY = """\
You are a senior legal analyst preparing a case briefing for a law firm partner.
Thoroughly analyze ALL available documents in this case and produce a comprehensive,
professional case summary in the exact markdown format below.

Be factual and precise. Cite specific document sections where relevant.
If a field is not available in the documents, write: *Not identified in available documents.*

---

## Case Overview
[2–3 sentences describing the nature, substance, and current status of this legal matter.]

## Parties

| Role | Name | Description |
|------|------|-------------|
| Plaintiff / Claimant | [Full name] | [Role or entity type] |
| Defendant / Respondent | [Full name] | [Role or entity type] |

*(Add rows for additional parties, counsel, or third parties if identified.)*

## Document Inventory

| Document | Type | Pages |
|----------|------|-------|
| [filename] | [document type] | [page count or "—"] |

## Nature of the Matter
[One paragraph explaining the legal theory, applicable jurisdiction, governing law,
and type of proceeding (e.g., commercial litigation, arbitration, contract dispute).]

## Key Claims & Allegations
1. [First cause of action or primary obligation]
2. [Second cause of action or obligation]
3. [Continue as needed]

## Relief Sought
[Enumerate all forms of relief requested — compensatory damages, punitive damages,
injunctive relief, declaratory relief, specific performance, attorneys' fees, costs.
Include specific monetary amounts where stated.]

## Critical Dates & Deadlines
- [Date]: [Event or deadline]
- [Date]: [Event or deadline]
*(Include filing dates, incident/breach dates, limitation periods, notice deadlines.)*

## Key Facts
- [Most legally significant factual allegation or admitted fact]
- [Continue as needed — focus on facts that directly support or undermine the claims]

## Strategic Assessment
[2–3 sentences identifying the key legal issues, evidentiary strengths and weaknesses,
litigation risks, and any immediate action items for the legal team.]

---
*Analysis based on documents uploaded to this case file.*\
"""


# ── Core function ──────────────────────────────────────────────────────────────

def generate_summary(case_id: str, verbose: bool = True, refresh: bool = False) -> dict:
    """
    Generate a professional case summary for the given case_id.

    Invokes the general_agent with a structured legal briefing prompt, then
    persists the result to Supabase.

    Args:
        case_id:  UUID of the case.
        verbose:  Print progress to stdout.
        refresh:  If True, delete any existing summary for this case before
                  saving the new one (prevents duplicates on re-run after
                  02_MIDDLE enriches the data).

    Returns:
        dict with keys: success (bool), summary_markdown, confidence,
                        provenance_links, and optionally error.
    """
    if verbose:
        print(f"\n[Summary] Generating case summary for case '{case_id}'...")

    graph      = build_graph()
    session_id = f"document_summary-{uuid.uuid4().hex[:8]}"
    config = {
        "configurable": {
            "thread_id": f"summary-{case_id}-{session_id}",
            "case_id":   case_id,
        }
    }

    state = {
        "messages":            [HumanMessage(content=_SUMMARY_QUERY)],
        "case_id":             case_id,
        "tool_call_count":     0,
        "search_results":      [],
        "kg_context":          [],
        "extractions_context": [],
        "provenance_links":    [],
        "reasoning_steps":     [],
        "needs_review":        False,
        "query_type":          "general",
        "agent_name":          None,
        "answer":              None,
        "confidence":          None,
    }

    try:
        result = graph.invoke(state, config=config)
    except Exception as e:
        if verbose:
            print(f"[Summary] ERROR: Agent invocation failed — {e}")
        return {"success": False, "error": str(e)}

    summary_markdown = result.get("answer", "")
    confidence       = result.get("confidence") or 0.0
    provenance_links = result.get("provenance_links", [])

    if not summary_markdown.strip():
        if verbose:
            print("[Summary] WARNING: Agent returned an empty response.")
        return {"success": False, "error": "Empty summary returned by agent."}

    if verbose:
        print(
            f"[Summary] Generated — {len(summary_markdown):,} chars "
            f"| confidence={confidence:.2f} "
            f"| sources={len(provenance_links)}"
        )

    _persist(
        case_id          = case_id,
        summary_markdown = summary_markdown,
        confidence       = confidence,
        provenance_links = provenance_links,
        verbose          = verbose,
        replace_existing = refresh,
    )

    return {
        "success":          True,
        "summary_markdown": summary_markdown,
        "confidence":       confidence,
        "provenance_links": provenance_links,
    }


# ── Persistence ────────────────────────────────────────────────────────────────

def _persist(
    case_id:          str,
    summary_markdown: str,
    confidence:       float,
    provenance_links: list,
    verbose:          bool = True,
    replace_existing: bool = False,
) -> None:
    """
    Save the summary to two locations in Supabase:
      1. agent_responses  — session_id = "document_summary" (frontend queryable)
      2. cases.ai_summary — direct column on the case row (see migration in docstring)
    Failures are non-fatal: the summary is still returned to the caller.

    If replace_existing=True, the old document_summary record is deleted first
    so the refresh after 02_MIDDLE doesn't accumulate duplicates.
    """
    try:
        from supabase import create_client
        sb = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    except Exception as e:
        if verbose:
            print(f"[Summary] WARNING: Supabase unavailable — summary not persisted. ({e})")
        return

    # 1 ── agent_responses ────────────────────────────────────────────────────
    try:
        if replace_existing:
            # Delete the previous summary so we don't accumulate duplicates
            sb.table("agent_responses") \
              .delete() \
              .eq("case_id", case_id) \
              .eq("session_id", "document_summary") \
              .execute()
            if verbose:
                print("[Summary] Removed previous summary record.")

        sb.table("agent_responses").insert({
            "case_id":          case_id,
            "session_id":       "document_summary",
            "query":            "DOCUMENT_SUMMARY",
            "agent_name":       "general_agent",
            "answer":           summary_markdown,
            "confidence":       round(float(confidence), 4),
            "needs_review":     confidence < 0.7,
            "provenance_links": provenance_links,
            "reasoning_steps":  [],
            "tool_calls_made":  [],
        }).execute()
        action = "Updated" if replace_existing else "Saved"
        if verbose:
            print(f"[Summary] {action} agent_responses (session_id='document_summary').")
    except Exception as e:
        if verbose:
            print(f"[Summary] WARNING: Could not save to agent_responses — {e}")

    # 2 ── cases.ai_summary (requires the ALTER TABLE migration) ──────────────
    try:
        from datetime import datetime, timezone
        sb.table("cases").update({
            "ai_summary":               summary_markdown,
            "ai_summary_confidence":    round(float(confidence), 4),
            "ai_summary_generated_at":  datetime.now(timezone.utc).isoformat(),
        }).eq("id", case_id).execute()
        if verbose:
            print("[Summary] Updated cases.ai_summary.")
    except Exception as e:
        if verbose:
            print(
                f"[Summary] NOTE: Could not update cases.ai_summary — {e}\n"
                f"[Summary]       Run the SQL migration in the module docstring "
                f"if this column does not exist yet."
            )


# ── Step tracking ──────────────────────────────────────────────────────────────

def _upsert_step(
    document_id: str | None,
    case_id: str,
    step_name: str,
    display_label: str,
    status: str,
) -> None:
    if not document_id:
        return
    try:
        from supabase import create_client
        from datetime import datetime, timezone
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            return
        sb = create_client(url, key)
        now = datetime.now(timezone.utc).isoformat()
        row: dict = {
            "document_id":   document_id,
            "case_id":       case_id,
            "step_name":     step_name,
            "display_label": display_label,
            "status":        status,
        }
        if status == "running":
            row["started_at"] = now
        if status in ("done", "error"):
            row["completed_at"] = now
        sb.table("document_processing_steps").upsert(
            row, on_conflict="document_id,step_name"
        ).execute()
    except Exception:
        pass  # non-fatal


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a professional case summary and persist it to Supabase."
    )
    parser.add_argument("--case_id", required=True, help="UUID of the case to summarise.")
    parser.add_argument("--document_id", default="",
                        help="UUID of the triggering document (for step tracking).")
    parser.add_argument(
        "--refresh", action="store_true",
        help="Replace the existing summary record instead of inserting a new one."
    )
    args = parser.parse_args()

    document_id = args.document_id or None
    _upsert_step(document_id, args.case_id, "initial_summary", "Case summary", "running")

    result = generate_summary(case_id=args.case_id, verbose=True, refresh=args.refresh)

    if result.get("success"):
        _upsert_step(document_id, args.case_id, "initial_summary", "Case summary", "done")
    else:
        _upsert_step(document_id, args.case_id, "initial_summary", "Case summary", "error")

    if result.get("success"):
        print("\n" + "=" * 60)
        print("  CASE SUMMARY")
        print("=" * 60)
        print(result["summary_markdown"])
        print("=" * 60)
        print(f"  Confidence : {result['confidence']:.2f}")
        src_count = len(result.get("provenance_links", []))
        if src_count:
            print(f"  Sources    : {src_count} document section(s)")
    else:
        print(f"\n[Summary] FAILED: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
