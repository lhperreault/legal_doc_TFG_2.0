"""
05_graph_analytics.py — Phase 2, Step 5: Graph Analytics

Importable functions + CLI wrapper for KG traversal operations.
Reads from kg_nodes and kg_edges. Does not write to Supabase.

Usage (CLI):
    python 05_graph_analytics.py --case_id "uuid" --mode timeline
    python 05_graph_analytics.py --case_id "uuid" --mode claim_evidence
    python 05_graph_analytics.py --case_id "uuid" --mode all
    python 05_graph_analytics.py --document_id "uuid" --mode timeline

Importable:
    from graph_analytics import build_timeline, find_claim_evidence_paths
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Edge types that move toward evidence/authority during BFS
# ---------------------------------------------------------------------------

EVIDENCE_EDGE_TYPES = {
    "supported_by", "references", "relies_on", "distinguishes",
    "supported_by_reverse", "references_reverse",
    "relies_on_reverse", "distinguishes_reverse",
}


# ---------------------------------------------------------------------------
# Supabase fetch helpers (CLI layer only)
# ---------------------------------------------------------------------------

def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _fetch_graph(supabase, case_id: str | None = None, document_id: str | None = None):
    """
    Fetch all kg_nodes and kg_edges for a case or document.
    Returns (nodes: list[dict], edges: list[dict])
    """
    query = supabase.table("kg_nodes").select("*")
    if case_id:
        query = query.eq("case_id", case_id)
    elif document_id:
        query = query.eq("document_id", document_id)
    nodes = query.execute().data or []

    node_ids = [n["id"] for n in nodes]
    edges: list[dict] = []
    for i in range(0, len(node_ids), 100):
        batch = node_ids[i:i + 100]
        resp = (
            supabase.table("kg_edges")
            .select("*")
            .in_("source_node_id", batch)
            .execute()
        )
        edges.extend(resp.data or [])

    return nodes, edges


def _fetch_sections_lookup(supabase, section_ids: list[str]) -> dict[str, dict]:
    """Fetch section titles and page ranges for provenance display."""
    lookup: dict[str, dict] = {}
    for i in range(0, len(section_ids), 100):
        batch = section_ids[i:i + 100]
        resp = (
            supabase.table("sections")
            .select("id, section_title, page_range, document_id")
            .in_("id", batch)
            .execute()
        )
        for row in (resp.data or []):
            lookup[row["id"]] = row
    return lookup


def _fetch_documents_lookup(supabase, document_ids: list[str]) -> dict[str, str]:
    """Fetch document file_names for display."""
    lookup: dict[str, str] = {}
    for i in range(0, len(document_ids), 100):
        batch = document_ids[i:i + 100]
        resp = (
            supabase.table("documents")
            .select("id, file_name")
            .in_("id", batch)
            .execute()
        )
        for row in (resp.data or []):
            lookup[row["id"]] = row["file_name"]
    return lookup


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_ISO_DATE_RE   = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_YEAR_ONLY_RE  = re.compile(r'^(\d{4})$')


def _make_sort_key(date_value: str | None, is_relative: bool) -> str:
    """Return an ISO-style string suitable for lexicographic date sorting."""
    if is_relative or not date_value:
        return "9999-99-99"
    dv = str(date_value).strip()
    if _ISO_DATE_RE.match(dv):
        return dv
    m = _YEAR_ONLY_RE.match(dv)
    if m:
        return f"{m.group(1)}-01-01"
    # Try extracting a 4-digit year from a longer string
    year_m = re.search(r'\b(1[89]\d{2}|20\d{2})\b', dv)
    if year_m:
        return f"{year_m.group(1)}-01-01"
    return "9999-99-99"


# ---------------------------------------------------------------------------
# Analytics Function 1: Timeline Generation
# ---------------------------------------------------------------------------

def build_timeline(
    nodes: list[dict],
    edges: list[dict],
    include_procedural: bool = True,
    party_filter: str | None = None,
) -> list[dict]:
    """
    Build a chronological timeline from event and procedural_event nodes.

    Args:
        nodes: all kg_nodes for the scope
        edges: all kg_edges for the scope
        include_procedural: if True, include procedural_event nodes (filings, rulings)
        party_filter: if set, only include events connected to this party name

    Returns:
        List of timeline entries sorted by date.
    """
    # Index nodes by id
    node_by_id = {n["id"]: n for n in nodes}

    # Build reverse adjacency: target_node_id → [source_node_id] for "involved_in" edges
    involved_in_sources: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.get("edge_type") == "involved_in":
            involved_in_sources[edge["target_node_id"]].append(edge["source_node_id"])

    # Filter to event nodes
    valid_types = {"event", "procedural_event"} if include_procedural else {"event"}
    event_nodes = [n for n in nodes if n.get("node_type") in valid_types]

    timeline: list[dict] = []
    skipped_no_date = 0

    for node in event_nodes:
        props      = node.get("properties") or {}
        date_value = props.get("date_value")
        is_relative = bool(props.get("is_relative", False))

        if not date_value:
            skipped_no_date += 1
            continue

        # Collect connected party names
        party_source_ids = involved_in_sources.get(node["id"], [])
        involved_parties = []
        for pid in party_source_ids:
            p = node_by_id.get(pid)
            if p and p.get("node_type") == "party":
                involved_parties.append(p["node_label"])

        # Apply party filter (substring match, case-insensitive)
        if party_filter:
            pf_lower = party_filter.lower()
            if not any(pf_lower in p.lower() for p in involved_parties):
                continue

        sort_key = _make_sort_key(date_value, is_relative)

        timeline.append({
            "date_value":         date_value,
            "date_sort_key":      sort_key,
            "event_label":        node["node_label"],
            "event_type":         node["node_type"],
            "node_id":            node["id"],
            "involved_parties":   involved_parties,
            "source_section_id":  node.get("source_section_id"),
            "source_document_id": node.get("document_id"),
            "confidence":         float(props.get("confidence", 0.8)),
            "is_relative":        is_relative,
            "reference_event":    props.get("reference_event"),
            "properties":         props,
        })

    if skipped_no_date:
        print(f"  Timeline: skipped {skipped_no_date} event nodes with no date_value")

    # Sort: by date_sort_key asc, then procedural_event before event on same date
    timeline.sort(key=lambda e: (
        e["date_sort_key"],
        0 if e["event_type"] == "procedural_event" else 1,
    ))

    return timeline


# ---------------------------------------------------------------------------
# Analytics Function 2: Claim → Evidence Path Finder
# ---------------------------------------------------------------------------

def find_claim_evidence_paths(
    nodes: list[dict],
    edges: list[dict],
    max_hops: int = 3,
    claim_filter: str | None = None,
) -> list[dict]:
    """
    For each claim node, find all paths to evidence/legal_authority nodes.

    Args:
        nodes: all kg_nodes for the scope
        edges: all kg_edges for the scope
        max_hops: maximum edge traversals (default 3)
        claim_filter: if set, only process claims whose label contains this string

    Returns:
        List of claim-evidence bundles, unsupported claims first.
    """
    node_by_id = {n["id"]: n for n in nodes}

    # Build bidirectional adjacency, only retaining evidence-direction edges
    adj: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for edge in edges:
        et   = edge.get("edge_type", "")
        src  = edge["source_node_id"]
        tgt  = edge["target_node_id"]
        conf = float(edge.get("confidence") or 0.5)
        if et in EVIDENCE_EDGE_TYPES:
            adj[src].append((tgt, et, conf))
        rev = et + "_reverse"
        if rev in EVIDENCE_EDGE_TYPES:
            adj[tgt].append((src, rev, conf))

    # Filter to claim nodes
    claim_nodes = [n for n in nodes if n.get("node_type") == "claim"]
    if claim_filter:
        cf_lower = claim_filter.lower()
        claim_nodes = [n for n in claim_nodes if cf_lower in n["node_label"].lower()]

    TERMINAL_TYPES = {"evidence", "legal_authority"}

    results: list[dict] = []

    for claim_node in claim_nodes:
        props = claim_node.get("properties") or {}

        # BFS from claim node
        # queue entries: (current_node_id, path_so_far, visited_set, min_conf_so_far)
        # path_so_far: alternating node/edge dicts
        initial_path = [{"node_id": claim_node["id"],
                         "node_label": claim_node["node_label"],
                         "node_type": "claim"}]
        queue: list[tuple[str, list, set, float]] = [
            (claim_node["id"], initial_path, {claim_node["id"]}, 1.0)
        ]
        evidence_paths: list[dict] = []

        while queue:
            cur_id, path, visited, min_conf = queue.pop(0)

            hop_count = len([p for p in path if "node_type" in p]) - 1
            if hop_count >= max_hops:
                continue

            for (nbr_id, et, edge_conf) in adj.get(cur_id, []):
                if nbr_id in visited:
                    continue
                nbr = node_by_id.get(nbr_id)
                if not nbr:
                    continue

                new_min = min(min_conf, edge_conf)
                new_path = path + [
                    {"edge_type": et, "confidence": edge_conf},
                    {"node_id": nbr_id,
                     "node_label": nbr["node_label"],
                     "node_type": nbr["node_type"]},
                ]

                if nbr["node_type"] in TERMINAL_TYPES:
                    evidence_paths.append({
                        "evidence_node_id": nbr_id,
                        "evidence_label":   nbr["node_label"],
                        "evidence_type":    nbr["node_type"],
                        "path":             new_path,
                        "hop_count":        hop_count + 1,
                        "path_confidence":  round(new_min, 3),
                    })
                else:
                    queue.append((nbr_id, new_path, visited | {nbr_id}, new_min))

        results.append({
            "claim_node_id":     claim_node["id"],
            "claim_label":       claim_node["node_label"],
            "claim_type":        props.get("claim_type", ""),
            "claim_confidence":  float(props.get("confidence", 0.8)),
            "source_section_id": claim_node.get("source_section_id"),
            "source_document_id": claim_node.get("document_id"),
            "evidence_paths":    evidence_paths,
            "unsupported":       len(evidence_paths) == 0,
        })

    # Sort: unsupported first, then by most evidence paths descending
    results.sort(key=lambda r: (0 if r["unsupported"] else 1, -len(r["evidence_paths"])))

    return results


# ---------------------------------------------------------------------------
# Output file writers
# ---------------------------------------------------------------------------

def _write_timeline_files(
    out_dir: str,
    scope: str,
    timeline: list[dict],
    sections_lookup: dict[str, dict],
    docs_lookup: dict[str, str],
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', '_', scope)

    with open(os.path.join(out_dir, f"{safe}_05_timeline.json"), "w", encoding="utf-8") as f:
        json.dump(timeline, f, indent=2, default=str)

    lines = ["# Case Timeline\n"]
    for entry in timeline:
        date_str   = entry["date_value"]
        label      = entry["event_label"]
        etype      = entry["event_type"]
        conf       = entry["confidence"]
        parties    = entry["involved_parties"]
        sec_id     = entry.get("source_section_id")
        doc_id     = entry.get("source_document_id")
        is_rel     = entry.get("is_relative", False)

        lines.append(f"## {date_str} — {label}")
        if is_rel:
            lines.append("**(relative date — exact timing unresolved)**")
        lines.append(f"**Type:** {etype} | **Confidence:** {conf}")
        if parties:
            lines.append(f"**Parties:** {', '.join(parties)}")

        # Provenance
        provenance_parts = []
        if sec_id and sec_id in sections_lookup:
            sec = sections_lookup[sec_id]
            title = sec.get("section_title") or ""
            pages = sec.get("page_range") or ""
            if title:
                provenance_parts.append(title)
            if pages:
                provenance_parts.append(f"pages {pages}")
        if doc_id and doc_id in docs_lookup:
            provenance_parts.insert(0, docs_lookup[doc_id])
        if provenance_parts:
            lines.append(f"**Source:** {' — '.join(provenance_parts)}")
        lines.append("")

    with open(os.path.join(out_dir, f"{safe}_05_timeline.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_claim_evidence_files(
    out_dir: str,
    scope: str,
    results: list[dict],
    sections_lookup: dict[str, dict],
    docs_lookup: dict[str, str],
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', '_', scope)

    with open(os.path.join(out_dir, f"{safe}_05_claim_evidence.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    unsupported = [r for r in results if r["unsupported"]]
    supported   = [r for r in results if not r["unsupported"]]

    lines = ["# Claim → Evidence Analysis\n"]

    if unsupported:
        lines.append(f"## UNSUPPORTED CLAIMS ({len(unsupported)} with no evidence linked)\n")
        for r in unsupported:
            lines.append(f"### \"{r['claim_label']}\"")
            if r.get("claim_type"):
                lines.append(f"- Claim type: {r['claim_type']}")
            lines.append(_provenance_str(r, sections_lookup, docs_lookup))
            lines.append("- **No evidence paths found**")
            lines.append("")

    if supported:
        lines.append(f"## SUPPORTED CLAIMS ({len(supported)} with evidence linked)\n")
        for r in supported:
            lines.append(f"### \"{r['claim_label']}\"")
            if r.get("claim_type"):
                lines.append(f"- Claim type: {r['claim_type']}")
            lines.append(_provenance_str(r, sections_lookup, docs_lookup))
            for ep in r["evidence_paths"]:
                path_labels = []
                for step in ep["path"]:
                    if "node_type" in step:
                        path_labels.append(f"{step['node_label']} ({step['node_type']})")
                    else:
                        path_labels.append(f"→[{step['edge_type']}]→")
                path_str = " ".join(path_labels)
                lines.append(
                    f"- Evidence path ({ep['hop_count']} hop(s), "
                    f"confidence: {ep['path_confidence']}): {path_str}"
                )
            lines.append("")

    with open(os.path.join(out_dir, f"{safe}_05_claim_evidence.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _provenance_str(
    result: dict,
    sections_lookup: dict[str, dict],
    docs_lookup: dict[str, str],
) -> str:
    sec_id = result.get("source_section_id")
    doc_id = result.get("source_document_id")
    parts  = []
    if doc_id and doc_id in docs_lookup:
        parts.append(docs_lookup[doc_id])
    if sec_id and sec_id in sections_lookup:
        sec   = sections_lookup[sec_id]
        title = sec.get("section_title") or ""
        pages = sec.get("page_range") or ""
        if title:
            parts.append(title)
        if pages:
            parts.append(f"pages {pages}")
    return f"- Source: {' — '.join(parts)}" if parts else "- Source: unknown"


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Graph analytics over KG.")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case_id",     help="UUID of the case")
    group.add_argument("--document_id", help="UUID of a single document")
    parser.add_argument(
        "--mode", required=True,
        choices=["timeline", "claim_evidence", "all"],
        help="Which analytics to run",
    )
    parser.add_argument("--party_filter", default=None,
                        help="Filter timeline events by party name substring")
    parser.add_argument("--claim_filter", default=None,
                        help="Filter claims by label substring")
    args = parser.parse_args()

    supabase = _get_supabase()

    # Determine scope label for output filenames
    scope = f"case_{args.case_id}" if args.case_id else f"doc_{args.document_id}"

    print(f"  Fetching KG graph for {scope}...")
    try:
        nodes, edges = _fetch_graph(
            supabase,
            case_id=args.case_id,
            document_id=args.document_id,
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch KG graph — {e}")
        sys.exit(1)

    if not nodes:
        print(f"SUCCESS: No KG nodes found for {scope}. Nothing to analyse.")
        return

    print(f"  {len(nodes)} nodes, {len(edges)} edges loaded")

    # Fetch provenance lookups
    section_ids  = list({n["source_section_id"] for n in nodes if n.get("source_section_id")})
    document_ids = list({n["document_id"] for n in nodes if n.get("document_id")})

    try:
        sections_lookup = _fetch_sections_lookup(supabase, section_ids)
        docs_lookup     = _fetch_documents_lookup(supabase, document_ids)
    except Exception as e:
        print(f"  WARNING: Could not fetch provenance lookups — {e}")
        sections_lookup, docs_lookup = {}, {}

    out_dir = os.path.join(os.path.dirname(__file__), '..', 'zz_temp_chunks')
    summary_parts: list[str] = []

    # --- Timeline ---
    if args.mode in ("timeline", "all"):
        print("  Running timeline generation...")
        timeline = build_timeline(
            nodes, edges,
            include_procedural=True,
            party_filter=args.party_filter,
        )
        _write_timeline_files(out_dir, scope, timeline, sections_lookup, docs_lookup)
        summary_parts.append(f"{len(timeline)} timeline entries")
        print(f"  Timeline: {len(timeline)} events")

    # --- Claim → Evidence ---
    if args.mode in ("claim_evidence", "all"):
        print("  Running claim→evidence path analysis...")
        results = find_claim_evidence_paths(
            nodes, edges,
            max_hops=3,
            claim_filter=args.claim_filter,
        )
        unsupported_count = sum(1 for r in results if r["unsupported"])
        _write_claim_evidence_files(out_dir, scope, results, sections_lookup, docs_lookup)
        summary_parts.append(
            f"{len(results)} claims ({unsupported_count} unsupported)"
        )
        print(f"  Claim-evidence: {len(results)} claims, {unsupported_count} unsupported")

    print(
        f"SUCCESS: Graph analytics complete. Mode: {args.mode}. "
        f"{', '.join(summary_parts)}. Output: {out_dir}/{scope}_05_*"
    )


if __name__ == "__main__":
    main()
