"""
tools/evidence_tools.py — Evidence linking tools for cross-document reasoning.

Matches evidence to allegations, elements, and counts using:
1. Explicit citations (parsed from allegation text)
2. Semantic similarity search across exhibit sections

Tables used:
- allegations, legal_elements, counts (source items to link)
- evidence_links (the links we create/query)
- documents (resolve exhibit labels)
- section_embeddings (semantic search)
"""

import os
import re
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from dotenv import load_dotenv
import openai

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Supabase + OpenAI helpers
# ---------------------------------------------------------------------------

def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


def _get_embedding(text: str) -> list[float]:
    """Generate embedding using OpenAI text-embedding-3-small."""
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000]
    )
    return response.data[0].embedding


def _extract_exhibit_references(text: str) -> list[str]:
    """
    Parse explicit exhibit citations from allegation/element text.
    
    Matches patterns like:
    - "Exhibit A", "Exhibit 1", "Ex. A", "Exh. B"
    - "(see Exhibit A)", "[Exhibit A attached hereto]"
    - "Exhibits A and B", "Exhibits A, B, and C"
    """
    patterns = [
        r'[Ee]xhibit\s+([A-Z0-9]+(?:\s*[-–]\s*[A-Z0-9]+)?)',  # Exhibit A, Exhibit A-1
        r'[Ee]xh?\.?\s+([A-Z0-9]+)',                           # Ex. A, Exh. B
        r'[Ee]xhibits?\s+([A-Z0-9]+(?:\s*,\s*[A-Z0-9]+)*(?:\s*(?:and|&)\s*[A-Z0-9]+)?)',  # Exhibits A, B, and C
    ]
    
    refs = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            # Split "A, B, and C" into individual refs
            parts = re.split(r'\s*[,&]\s*|\s+and\s+', match)
            for part in parts:
                part = part.strip()
                if part:
                    refs.add(f"Exhibit {part}")
    
    return list(refs)


def _resolve_exhibit_label_to_document(case_id: str, exhibit_label: str) -> dict | None:
    """
    Convert an exhibit label like "Exhibit A" to an actual document record.
    
    Checks:
    1. documents.exhibit_label field (exact match)
    2. documents.filename containing the exhibit reference
    """
    sb = _get_supabase()
    
    # Normalize: "Exhibit A" -> extract just "A"
    match = re.search(r'[Ee]xh?i?b?i?t?\s*\.?\s*([A-Z0-9]+(?:\s*[-–]\s*[A-Z0-9]+)?)', exhibit_label)
    letter = match.group(1).strip() if match else exhibit_label.strip()
    
    # Try exact match on exhibit_label
    resp = (
        sb.table("documents")
        .select("id, filename, document_type")
        .eq("case_id", case_id)
        .or_(f"exhibit_label.ilike.%{letter}%,exhibit_label.ilike.%exhibit {letter}%")
        .limit(1)
        .execute()
    )
    
    if resp.data:
        return resp.data[0]
    
    # Fallback: filename contains exhibit + letter
    resp = (
        sb.table("documents")
        .select("id, filename, document_type")
        .eq("case_id", case_id)
        .or_(f"filename.ilike.%exhibit%{letter}%,filename.ilike.%ex%{letter}%")
        .limit(1)
        .execute()
    )
    
    return resp.data[0] if resp.data else None


def _vector_search(
    case_id: str,
    query_embedding: list[float],
    document_types: list[str] | None = None,
    limit: int = 5,
    similarity_threshold: float = 0.3
) -> list[dict]:
    """
    Perform vector similarity search on section_embeddings.
    
    Uses the match_sections_by_embedding RPC function.
    """
    sb = _get_supabase()
    
    params = {
        "query_embedding": query_embedding,
        "match_case_id": case_id,
        "match_threshold": similarity_threshold,
        "match_count": limit,
    }
    
    if document_types:
        params["filter_doc_types"] = document_types
    
    try:
        result = sb.rpc("match_sections_by_embedding", params).execute()
        return result.data or []
    except Exception as e:
        # If RPC doesn't exist yet, return empty (graceful degradation)
        print(f"[evidence_tools] Vector search RPC failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def match_evidence(
    target_id: str,
    target_type: Literal["allegation", "element", "count"] = "allegation",
    include_implicit: bool = True,
    config: RunnableConfig = None,
) -> str:
    """Find evidence that supports or contradicts an allegation, element, or count.

    Performs two types of matching:
    1. EXPLICIT: Parses "see Exhibit A" from text and resolves to actual documents
    2. IMPLICIT: Semantic search across exhibits for supporting content

    Args:
        target_id: UUID of the allegation, element, or count
        target_type: What kind of item this is ("allegation", "element", "count")
        include_implicit: If True, also do semantic search (not just explicit citations)

    Returns:
        Formatted list of evidence matches with confidence and snippets.
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    sb = _get_supabase()

    # ---------------------------------------------------------------------------
    # 1. Fetch the target item based on type
    # ---------------------------------------------------------------------------
    if target_type == "allegation":
        resp = (
            sb.table("allegations")
            .select("id, allegation_text, document_id, count_id, section_id")
            .eq("id", target_id)
            .single()
            .execute()
        )
        if not resp.data:
            return f"ERROR: No allegation found with id {target_id}"
        target = resp.data
        search_text = target.get("allegation_text", "")
        
    elif target_type == "element":
        resp = (
            sb.table("legal_elements")
            .select("id, element_text, element_label, document_id, count_id, section_id")
            .eq("id", target_id)
            .single()
            .execute()
        )
        if not resp.data:
            return f"ERROR: No legal_element found with id {target_id}"
        target = resp.data
        search_text = target.get("element_text") or target.get("element_label", "")
        
    elif target_type == "count":
        resp = (
            sb.table("counts")
            .select("id, count_label, summary, document_id, section_id")
            .eq("id", target_id)
            .single()
            .execute()
        )
        if not resp.data:
            return f"ERROR: No count found with id {target_id}"
        target = resp.data
        search_text = target.get("summary") or target.get("count_label", "")
    else:
        return f"ERROR: Invalid target_type '{target_type}'. Use: allegation, element, count"

    results = []
    
    # ---------------------------------------------------------------------------
    # 2. Extract and resolve EXPLICIT exhibit citations
    # ---------------------------------------------------------------------------
    explicit_refs = _extract_exhibit_references(search_text)
    
    for ref in explicit_refs:
        doc = _resolve_exhibit_label_to_document(case_id, ref)
        if doc:
            results.append({
                "link_type": "explicit_citation",
                "evidence_reference": ref,
                "evidence_document_id": doc["id"],
                "evidence_document_name": doc.get("filename", ""),
                "confidence": 0.95,
                "relationship": "supports",
                "snippet": f"[Cited as '{ref}' in the {target_type}]",
                "page": None,
                "resolved": True,
            })
        else:
            results.append({
                "link_type": "explicit_citation",
                "evidence_reference": ref,
                "evidence_document_id": None,
                "confidence": 0.0,
                "relationship": "unknown",
                "snippet": f"[Citation '{ref}' not found in uploaded documents]",
                "resolved": False,
            })

    # ---------------------------------------------------------------------------
    # 3. Find IMPLICIT evidence via semantic search
    # ---------------------------------------------------------------------------
    if include_implicit and search_text and len(search_text) > 20:
        try:
            query_embedding = _get_embedding(search_text)
            
            # Search exhibits, contracts, declarations — not complaints
            semantic_matches = _vector_search(
                case_id=case_id,
                query_embedding=query_embedding,
                document_types=["exhibit", "contract", "evidence", "declaration", "email", "agreement"],
                limit=5,
                similarity_threshold=0.35
            )
            
            for match in semantic_matches:
                # Skip if already found via explicit citation
                if any(r.get("evidence_document_id") == match.get("document_id") for r in results):
                    continue
                
                results.append({
                    "link_type": "agent_discovered",
                    "evidence_document_id": match.get("document_id"),
                    "evidence_section_id": match.get("section_id"),
                    "evidence_document_name": match.get("document_name", ""),
                    "confidence": round(match.get("similarity", 0.5), 3),
                    "relationship": "supports",
                    "snippet": (match.get("search_text", "") or "")[:300],
                    "page": match.get("page_range"),
                    "section_title": match.get("section_title"),
                    "resolved": True,
                })
        except Exception as e:
            results.append({
                "link_type": "error",
                "error": f"Semantic search failed: {e}",
            })

    # ---------------------------------------------------------------------------
    # 4. Format output
    # ---------------------------------------------------------------------------
    if not results:
        return (
            f"No evidence found for {target_type} [{target_id[:8]}].\n\n"
            f"Text searched: \"{search_text[:200]}{'...' if len(search_text) > 200 else ''}\""
        )

    lines = [f"Evidence Matches for {target_type.title()} ({len(results)} found)\n"]
    lines.append(f"Target: \"{search_text[:120]}{'...' if len(search_text) > 120 else ''}\"\n")

    for i, r in enumerate(results, 1):
        link_type = r.get("link_type", "unknown")
        conf = r.get("confidence", 0)
        rel = r.get("relationship", "unknown")
        doc_name = r.get("evidence_document_name", "Unknown")
        snippet = r.get("snippet", "")
        page = r.get("page")
        resolved = r.get("resolved", False)
        
        status = "✓" if resolved and conf >= 0.5 else "?" if resolved else "✗"
        page_str = f" (p. {page})" if page else ""
        
        lines.append(f"{i}. [{status}] {doc_name}{page_str}")
        lines.append(f"   Type: {link_type} | Confidence: {conf:.2f} | Relationship: {rel}")
        if snippet and not snippet.startswith("["):
            lines.append(f"   Snippet: \"{snippet[:180]}{'...' if len(snippet) > 180 else ''}\"")
        elif snippet:
            lines.append(f"   {snippet}")
        if r.get("error"):
            lines.append(f"   ⚠ {r['error']}")
        lines.append("")

    return "\n".join(lines)


@tool
def detect_evidence_gaps(
    scope: Literal["allegations", "elements", "counts", "all"] = "all",
    config: RunnableConfig = None,
) -> str:
    """Find allegations, elements, or counts that have NO evidence linked.

    Use this to identify gaps — claims that need supporting evidence.

    Args:
        scope: What to check: "allegations", "elements", "counts", or "all"

    Returns:
        List of unlinked items grouped by type.
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    sb = _get_supabase()
    gaps = {}

    # ---------------------------------------------------------------------------
    # Check allegations
    # ---------------------------------------------------------------------------
    if scope in ("allegations", "all"):
        # Get all allegations for this case (via documents)
        docs_resp = sb.table("documents").select("id").eq("case_id", case_id).execute()
        doc_ids = [d["id"] for d in (docs_resp.data or [])]
        
        if doc_ids:
            allegations_resp = (
                sb.table("allegations")
                .select("id, allegation_text, allegation_number, count_id")
                .in_("document_id", doc_ids)
                .execute()
            )
            allegations = allegations_resp.data or []
            
            if allegations:
                allegation_ids = [a["id"] for a in allegations]
                linked_resp = (
                    sb.table("evidence_links")
                    .select("allegation_id")
                    .in_("allegation_id", allegation_ids)
                    .not_.is_("evidence_document_id", "null")
                    .execute()
                )
                linked_ids = {l["allegation_id"] for l in (linked_resp.data or [])}
                
                unlinked = [a for a in allegations if a["id"] not in linked_ids]
                if unlinked:
                    gaps["allegations"] = unlinked

    # ---------------------------------------------------------------------------
    # Check legal elements
    # ---------------------------------------------------------------------------
    if scope in ("elements", "all"):
        docs_resp = sb.table("documents").select("id").eq("case_id", case_id).execute()
        doc_ids = [d["id"] for d in (docs_resp.data or [])]
        
        if doc_ids:
            elements_resp = (
                sb.table("legal_elements")
                .select("id, element_label, element_text, count_id")
                .in_("document_id", doc_ids)
                .execute()
            )
            elements = elements_resp.data or []
            
            if elements:
                element_ids = [e["id"] for e in elements]
                linked_resp = (
                    sb.table("evidence_links")
                    .select("element_id")
                    .in_("element_id", element_ids)
                    .not_.is_("evidence_document_id", "null")
                    .execute()
                )
                linked_ids = {l["element_id"] for l in (linked_resp.data or [])}
                
                unlinked = [e for e in elements if e["id"] not in linked_ids]
                if unlinked:
                    gaps["elements"] = unlinked

    # ---------------------------------------------------------------------------
    # Check counts
    # ---------------------------------------------------------------------------
    if scope in ("counts", "all"):
        counts_resp = (
            sb.table("counts")
            .select("id, count_label, count_number, summary")
            .eq("case_id", case_id)
            .execute()
        )
        counts = counts_resp.data or []
        
        if counts:
            count_ids = [c["id"] for c in counts]
            linked_resp = (
                sb.table("evidence_links")
                .select("count_id")
                .in_("count_id", count_ids)
                .not_.is_("evidence_document_id", "null")
                .execute()
            )
            linked_ids = {l["count_id"] for l in (linked_resp.data or [])}
            
            unlinked = [c for c in counts if c["id"] not in linked_ids]
            if unlinked:
                gaps["counts"] = unlinked

    # ---------------------------------------------------------------------------
    # Format output
    # ---------------------------------------------------------------------------
    if not gaps:
        return f"No evidence gaps found. All {scope} have at least one evidence link."

    total = sum(len(v) for v in gaps.values())
    lines = [f"Evidence Gaps: {total} item(s) without resolved evidence links\n"]

    if "counts" in gaps:
        lines.append(f"## Counts ({len(gaps['counts'])})")
        for c in gaps["counts"][:10]:
            label = c.get("count_label", "Unknown")
            num = c.get("count_number", "?")
            lines.append(f"  • Count {num}: {label[:60]}")
        lines.append("")

    if "elements" in gaps:
        lines.append(f"## Legal Elements ({len(gaps['elements'])})")
        for e in gaps["elements"][:10]:
            label = e.get("element_label") or e.get("element_text", "")[:50]
            lines.append(f"  • [{e['id'][:8]}] {label[:60]}")
        if len(gaps["elements"]) > 10:
            lines.append(f"  ... and {len(gaps['elements']) - 10} more")
        lines.append("")

    if "allegations" in gaps:
        lines.append(f"## Allegations ({len(gaps['allegations'])})")
        for a in gaps["allegations"][:10]:
            num = a.get("allegation_number", "?")
            text = a.get("allegation_text", "")[:60]
            lines.append(f"  • ¶{num}: \"{text}...\"")
        if len(gaps["allegations"]) > 10:
            lines.append(f"  ... and {len(gaps['allegations']) - 10} more")
        lines.append("")

    return "\n".join(lines)


@tool
def link_evidence_batch(
    target_type: Literal["allegations", "elements"] = "allegations",
    dry_run: bool = True,
    config: RunnableConfig = None,
) -> str:
    """Process all unlinked allegations or elements and attempt to find evidence.

    Batch operation — use after uploading new exhibits or after extraction.
    Writes to evidence_links table.

    Args:
        target_type: What to process: "allegations" or "elements"
        dry_run: If True (default), report what would be linked without writing.
                 Set False to actually create evidence_links.

    Returns:
        Summary of links found/created.
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    sb = _get_supabase()

    # Get documents for this case
    docs_resp = sb.table("documents").select("id").eq("case_id", case_id).execute()
    doc_ids = [d["id"] for d in (docs_resp.data or [])]
    
    if not doc_ids:
        return "No documents found for this case."

    # ---------------------------------------------------------------------------
    # 1. Fetch unlinked items
    # ---------------------------------------------------------------------------
    if target_type == "allegations":
        items_resp = (
            sb.table("allegations")
            .select("id, allegation_text, document_id")
            .in_("document_id", doc_ids)
            .execute()
        )
        items = items_resp.data or []
        id_field = "allegation_id"
        text_field = "allegation_text"
    else:  # elements
        items_resp = (
            sb.table("legal_elements")
            .select("id, element_text, element_label, document_id")
            .in_("document_id", doc_ids)
            .execute()
        )
        items = items_resp.data or []
        id_field = "element_id"
        text_field = "element_text"

    if not items:
        return f"No {target_type} found for this case."

    # Find which are already linked
    item_ids = [i["id"] for i in items]
    linked_resp = (
        sb.table("evidence_links")
        .select(id_field)
        .in_(id_field, item_ids)
        .not_.is_("evidence_document_id", "null")
        .execute()
    )
    linked_ids = {l[id_field] for l in (linked_resp.data or [])}
    
    unlinked = [i for i in items if i["id"] not in linked_ids]
    
    if not unlinked:
        return f"All {target_type} already have evidence links."

    # ---------------------------------------------------------------------------
    # 2. Process each unlinked item
    # ---------------------------------------------------------------------------
    stats = {"processed": 0, "explicit_found": 0, "implicit_found": 0, "written": 0, "errors": []}
    links_to_create = []

    for item in unlinked[:25]:  # Cap at 25 to avoid timeout
        stats["processed"] += 1
        text = item.get(text_field) or item.get("element_label", "")
        
        # Extract explicit citations
        explicit_refs = _extract_exhibit_references(text)
        for ref in explicit_refs:
            doc = _resolve_exhibit_label_to_document(case_id, ref)
            if doc:
                stats["explicit_found"] += 1
                links_to_create.append({
                    "document_id": item["document_id"],
                    id_field: item["id"],
                    "evidence_reference": ref,
                    "evidence_document_id": doc["id"],
                    "link_type": "explicit_citation",
                    "relationship": "supports",
                    "confidence_score": 0.95,
                    "created_by": "cross_doc_agent",
                })

        # Semantic search
        if len(text) > 20:
            try:
                query_embedding = _get_embedding(text)
                matches = _vector_search(
                    case_id=case_id,
                    query_embedding=query_embedding,
                    document_types=["exhibit", "contract", "evidence", "declaration"],
                    limit=2,
                    similarity_threshold=0.4
                )
                for m in matches:
                    # Avoid duplicates
                    if any(l.get("evidence_document_id") == m.get("document_id") for l in links_to_create):
                        continue
                    stats["implicit_found"] += 1
                    links_to_create.append({
                        "document_id": item["document_id"],
                        id_field: item["id"],
                        "evidence_reference": f"[Semantic match: {m.get('section_title', 'section')}]",
                        "evidence_document_id": m.get("document_id"),
                        "evidence_section_id": m.get("section_id"),
                        "evidence_snippet": (m.get("search_text", "") or "")[:500],
                        "evidence_page": m.get("page_range"),
                        "link_type": "agent_discovered",
                        "relationship": "supports",
                        "confidence_score": round(m.get("similarity", 0.5), 3),
                        "created_by": "cross_doc_agent",
                    })
            except Exception as e:
                stats["errors"].append(f"{item['id'][:8]}: {e}")

    # ---------------------------------------------------------------------------
    # 3. Write to database (if not dry run)
    # ---------------------------------------------------------------------------
    if not dry_run and links_to_create:
        try:
            sb.table("evidence_links").insert(links_to_create).execute()
            stats["written"] = len(links_to_create)
        except Exception as e:
            stats["errors"].append(f"Insert failed: {e}")

    # ---------------------------------------------------------------------------
    # 4. Format output
    # ---------------------------------------------------------------------------
    mode = "DRY RUN" if dry_run else "EXECUTED"
    lines = [f"Evidence Linking Batch [{mode}] — {target_type}\n"]
    lines.append(f"Items processed: {stats['processed']} / {len(unlinked)} unlinked")
    lines.append(f"Explicit citations resolved: {stats['explicit_found']}")
    lines.append(f"Semantic matches found: {stats['implicit_found']}")
    lines.append(f"Total links: {len(links_to_create)}")
    
    if not dry_run:
        lines.append(f"Links written to database: {stats['written']}")
    
    if stats["errors"]:
        lines.append(f"\nErrors ({len(stats['errors'])}):")
        for err in stats["errors"][:5]:
            lines.append(f"  • {err}")

    if dry_run and links_to_create:
        lines.append(f"\n→ Run with dry_run=False to create {len(links_to_create)} evidence links.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

evidence_tools = [
    match_evidence,
    detect_evidence_gaps,
    link_evidence_batch,
]