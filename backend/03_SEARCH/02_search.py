"""
02_search.py — Phase 3: Hybrid Search Module

Importable core search function + CLI wrapper for testing.
Calls the hybrid_search SQL function in Supabase, which combines:
  - Semantic similarity (pgvector cosine distance)
  - Keyword relevance (pg_trgm trigram matching)
  - Structural filters (document_type, semantic_label, level, document_ids)

All searches are scoped to a case_id — cross-case search is intentionally blocked.

Importable usage:
    import sys
    sys.path.append("backend/03_SEARCH")
    from search import hybrid_search

    results = hybrid_search(
        query="payment deadline",
        case_id="uuid-...",
        document_types=["Contract - NDA"],
        limit=5,
    )

CLI usage:
    python 02_search.py --case_id "uuid-..." --query "payment deadline"
    python 02_search.py --case_id "uuid-..." --query "breach of fiduciary duty" --doc_types "Pleading - Complaint"
    python 02_search.py --case_id "uuid-..." --query "Exhibit A" --labels "exhibit_reference"
    python 02_search.py --case_id "uuid-..." --query "governing law" --limit 5 --semantic_weight 0.8
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL    = "text-embedding-3-small"
DEFAULT_LIMIT      = 10
DEFAULT_WEIGHT     = 0.7    # semantic weight (1 - weight goes to keyword)
DEFAULT_THRESHOLD  = 0.3    # minimum cosine similarity to include


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _get_clients():
    from openai import OpenAI
    from supabase import create_client

    sb_url  = os.getenv("SUPABASE_URL")
    sb_key  = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    oai_key = os.getenv("OPENAI_API_KEY")

    missing = [k for k, v in [
        ("SUPABASE_URL", sb_url),
        ("SUPABASE_SERVICE_ROLE_KEY", sb_key),
        ("OPENAI_API_KEY", oai_key),
    ] if not v]

    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    return create_client(sb_url, sb_key), OpenAI(api_key=oai_key)


# ---------------------------------------------------------------------------
# Query embedding
# ---------------------------------------------------------------------------

def _embed_query(openai_client, query: str) -> list[float]:
    """Embed the search query using the same model as the indexed sections."""
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[query],
    )
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Section text + provenance fetch
# ---------------------------------------------------------------------------

def _fetch_section_details(supabase, section_ids: list[str]) -> dict[str, dict]:
    """
    Fetch full section_text, anchor_id, start_page, end_page for each section_id.
    Also fetches parent section title for context.
    Returns dict: section_id → detail dict.
    """
    if not section_ids:
        return {}

    details: dict[str, dict] = {}
    for i in range(0, len(section_ids), 100):
        batch = section_ids[i:i + 100]
        resp = (
            supabase.table("sections")
            .select("id, section_text, anchor_id, start_page, end_page, parent_section_id")
            .in_("id", batch)
            .execute()
        )
        for row in (resp.data or []):
            details[row["id"]] = row

    # Fetch parent titles for context
    parent_ids = list({
        d["parent_section_id"]
        for d in details.values()
        if d.get("parent_section_id")
    })
    parent_titles: dict[str, str] = {}
    for i in range(0, len(parent_ids), 100):
        batch = parent_ids[i:i + 100]
        resp = (
            supabase.table("sections")
            .select("id, section_title, section_text")
            .in_("id", batch)
            .execute()
        )
        for row in (resp.data or []):
            title   = row.get("section_title") or ""
            snippet = (row.get("section_text") or "")[:200]
            parent_titles[row["id"]] = title if title else snippet[:80]

    for sec_id, detail in details.items():
        pid = detail.get("parent_section_id")
        detail["parent_context"] = parent_titles.get(pid) if pid else None

    return details


def _fetch_document_names(supabase, document_ids: list[str]) -> dict[str, str]:
    """Fetch file_name for each document_id."""
    if not document_ids:
        return {}
    names: dict[str, str] = {}
    for i in range(0, len(document_ids), 100):
        batch = document_ids[i:i + 100]
        resp = (
            supabase.table("documents")
            .select("id, file_name")
            .in_("id", batch)
            .execute()
        )
        for row in (resp.data or []):
            names[row["id"]] = row["file_name"]
    return names


# ---------------------------------------------------------------------------
# Core search function (importable)
# ---------------------------------------------------------------------------

def hybrid_search(
    query: str,
    case_id: str,
    document_types: list[str] | None = None,
    semantic_labels: list[str] | None = None,
    document_ids: list[str] | None = None,
    min_level: int | None = None,
    max_level: int | None = None,
    limit: int = DEFAULT_LIMIT,
    semantic_weight: float = DEFAULT_WEIGHT,
    similarity_threshold: float = DEFAULT_THRESHOLD,
    include_text: bool = True,
    supabase=None,
    openai_client=None,
) -> dict:
    """
    Hybrid semantic + keyword search over section embeddings for a case.

    Args:
        query:               The search query string.
        case_id:             UUID of the case to search within (mandatory).
        document_types:      Optional list of document_type values to filter by.
        semantic_labels:     Optional list of semantic_label values to filter by.
        document_ids:        Optional list of document UUIDs to restrict search to.
        min_level / max_level: Hierarchy depth filter (0 = top-level sections).
        limit:               Maximum number of results to return.
        semantic_weight:     0.0–1.0. Weight for vector score vs keyword score.
        similarity_threshold: Minimum cosine similarity (0.0–1.0) to include a result.
        include_text:        If True, fetches full section_text for each result.
        supabase:            Optional pre-initialized Supabase client (avoids re-init).
        openai_client:       Optional pre-initialized OpenAI client.

    Returns:
        {
            "query": str,
            "case_id": str,
            "filters_applied": {...},
            "results": [ {...}, ... ],
            "total_results": int,
        }
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not case_id:
        raise ValueError("case_id is required — all searches must be case-scoped")

    # Initialize clients if not provided
    if supabase is None or openai_client is None:
        _sb, _oai = _get_clients()
        supabase      = supabase or _sb
        openai_client = openai_client or _oai

    # Embed the query
    query_embedding = _embed_query(openai_client, query.strip())

    # Call the hybrid_search SQL function via Supabase RPC
    rpc_params = {
        "query_embedding":      query_embedding,
        "query_text":           query.strip(),
        "p_case_id":            case_id,
        "p_document_types":     document_types,
        "p_semantic_labels":    semantic_labels,
        "p_document_ids":       document_ids,
        "p_min_level":          min_level,
        "p_max_level":          max_level,
        "p_limit":              limit,
        "p_semantic_weight":    semantic_weight,
        "p_similarity_threshold": similarity_threshold,
    }
    # Remove None values — Supabase RPC treats absent keys as SQL NULL defaults
    rpc_params = {k: v for k, v in rpc_params.items() if v is not None}

    try:
        rpc_resp = supabase.rpc("hybrid_search", rpc_params).execute()
    except Exception as e:
        raise RuntimeError(f"hybrid_search RPC failed: {e}") from e

    raw_results = rpc_resp.data or []

    # Fetch full section details + document names if needed
    section_ids  = [r["section_id"] for r in raw_results]
    document_ids_found = list({r["document_id"] for r in raw_results})

    section_details = _fetch_section_details(supabase, section_ids) if include_text else {}
    doc_names       = _fetch_document_names(supabase, document_ids_found)

    # Build structured result list
    results = []
    for r in raw_results:
        sec_id  = r["section_id"]
        doc_id  = r["document_id"]
        details = section_details.get(sec_id, {})

        results.append({
            "section_id":     sec_id,
            "document_id":    doc_id,
            "file_name":      doc_names.get(doc_id, ""),
            "document_type":  r.get("document_type"),
            "section_title":  r.get("section_title"),
            "semantic_label": r.get("semantic_label"),
            "level":          r.get("level"),
            "page_range":     r.get("page_range"),
            "is_synthetic":   r.get("is_synthetic"),
            "parent_context": details.get("parent_context"),
            "section_text":   details.get("section_text") if include_text else None,
            "scores": {
                "semantic": round(float(r.get("semantic_score") or 0), 4),
                "keyword":  round(float(r.get("keyword_score") or 0), 4),
                "combined": round(float(r.get("combined_score") or 0), 4),
            },
            "provenance": {
                "anchor_id":  details.get("anchor_id"),
                "is_synthetic": r.get("is_synthetic"),
                "start_page": details.get("start_page"),
                "end_page":   details.get("end_page"),
            },
        })

    return {
        "query":          query,
        "case_id":        case_id,
        "filters_applied": {
            "document_types":  document_types,
            "semantic_labels": semantic_labels,
            "document_ids":    document_ids,
            "min_level":       min_level,
            "max_level":       max_level,
            "semantic_weight": semantic_weight,
            "threshold":       similarity_threshold,
        },
        "results":       results,
        "total_results": len(results),
    }


# ---------------------------------------------------------------------------
# CLI wrapper (for manual testing)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hybrid search over case sections. For testing — agents import hybrid_search() directly."
    )
    parser.add_argument("--case_id",        required=True, help="UUID of the case to search")
    parser.add_argument("--query",          required=True, help="Search query string")
    parser.add_argument("--doc_types",      default=None,
                        help="Comma-separated list of document_type values to filter by")
    parser.add_argument("--labels",         default=None,
                        help="Comma-separated list of semantic_label values to filter by")
    parser.add_argument("--doc_ids",        default=None,
                        help="Comma-separated list of document UUIDs to restrict search to")
    parser.add_argument("--min_level",      type=int, default=None)
    parser.add_argument("--max_level",      type=int, default=None)
    parser.add_argument("--limit",          type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--semantic_weight",type=float, default=DEFAULT_WEIGHT,
                        help="0.0–1.0 weight for semantic vs keyword score (default 0.7)")
    parser.add_argument("--threshold",      type=float, default=DEFAULT_THRESHOLD,
                        help="Minimum cosine similarity to include (default 0.3)")
    parser.add_argument("--no_text",        action="store_true",
                        help="Skip fetching section_text (faster for score inspection)")
    parser.add_argument("--json",           action="store_true",
                        help="Output raw JSON instead of formatted display")
    args = parser.parse_args()

    # Parse comma-separated filter lists
    doc_types = [x.strip() for x in args.doc_types.split(",")] if args.doc_types else None
    labels    = [x.strip() for x in args.labels.split(",")]    if args.labels    else None
    doc_ids   = [x.strip() for x in args.doc_ids.split(",")]   if args.doc_ids   else None

    try:
        output = hybrid_search(
            query=args.query,
            case_id=args.case_id,
            document_types=doc_types,
            semantic_labels=labels,
            document_ids=doc_ids,
            min_level=args.min_level,
            max_level=args.max_level,
            limit=args.limit,
            semantic_weight=args.semantic_weight,
            similarity_threshold=args.threshold,
            include_text=not args.no_text,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if args.json:
        print(json.dumps(output, indent=2, default=str))
        return

    # Human-readable display
    print(f"\n{'='*60}")
    print(f"  Search: \"{output['query']}\"")
    print(f"  Case:   {output['case_id']}")
    print(f"  Found:  {output['total_results']} result(s)")
    print(f"{'='*60}\n")

    for i, r in enumerate(output["results"], 1):
        print(f"[{i}] {r['section_title'] or '(untitled)'}")
        print(f"     File:   {r['file_name']} | Type: {r['document_type']}")
        print(f"     Label:  {r['semantic_label']} | Level: {r['level']} | Pages: {r['page_range']}")
        print(f"     Scores: semantic={r['scores']['semantic']:.3f}  "
              f"keyword={r['scores']['keyword']:.3f}  "
              f"combined={r['scores']['combined']:.3f}")
        if r.get("parent_context"):
            print(f"     Parent: {r['parent_context'][:80]}")
        if r.get("section_text") and not args.no_text:
            snippet = r["section_text"][:300].replace("\n", " ")
            print(f"     Text:   {snippet}{'...' if len(r['section_text']) > 300 else ''}")
        print()


if __name__ == "__main__":
    main()
