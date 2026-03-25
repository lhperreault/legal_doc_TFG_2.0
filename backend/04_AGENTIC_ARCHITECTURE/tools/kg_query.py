"""
tools/kg_query.py — KG tools: query_kg, get_claim_evidence, get_timeline

Wraps 02_MIDDLE/05_graph_analytics.py functions and direct Supabase KG queries.
case_id is injected at runtime via LangChain RunnableConfig.
"""

import os
import sys

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))

# Path to 02_MIDDLE (for graph_analytics)
_MIDDLE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '02_MIDDLE')
)
if _MIDDLE_DIR not in sys.path:
    sys.path.insert(0, _MIDDLE_DIR)

import importlib.util as _ilu

_ga_spec = _ilu.spec_from_file_location(
    "graph_analytics",
    os.path.join(_MIDDLE_DIR, "05_graph_analytics.py"),
)
_ga_mod = _ilu.module_from_spec(_ga_spec)
_ga_spec.loader.exec_module(_ga_mod)
build_timeline           = _ga_mod.build_timeline
find_claim_evidence_paths = _ga_mod.find_claim_evidence_paths


# ---------------------------------------------------------------------------
# Supabase helper
# ---------------------------------------------------------------------------

def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


def _fetch_kg_graph(case_id: str) -> tuple[list[dict], list[dict]]:
    """Fetch all KG nodes and edges for a case from Supabase."""
    sb = _get_supabase()

    nodes_resp = (
        sb.table("kg_nodes")
        .select("id, document_id, node_type, node_label, properties, source_section_id")
        .eq("case_id", case_id)
        .execute()
    )
    nodes = nodes_resp.data or []

    node_ids = [n["id"] for n in nodes]
    if node_ids:
        edges_resp = (
            sb.table("kg_edges")
            .select("id, source_node_id, target_node_id, edge_type, edge_scope, confidence, properties")
            .in_("source_node_id", node_ids)
            .execute()
        )
        edges = edges_resp.data or []
    else:
        edges = []

    return nodes, edges


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def get_claim_evidence(
    claim_filter: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Find evidence paths for claims in the complaint using the knowledge graph.

    Traces paths from claim nodes to supporting evidence and legal authority nodes.
    Shows which claims have evidence support and which are unsupported.

    Args:
        claim_filter: Optional substring to filter claims by label (e.g. "breach", "antitrust", "tying").
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    try:
        nodes, edges = _fetch_kg_graph(case_id)
    except Exception as e:
        return f"ERROR: Could not fetch KG data — {e}"

    if not nodes:
        return "No knowledge graph nodes found for this case."

    try:
        results = find_claim_evidence_paths(nodes, edges, claim_filter=claim_filter)
    except Exception as e:
        return f"ERROR: Graph analytics failed — {e}"

    if not results:
        return "No claim-evidence paths found."

    lines = [f"Claim-Evidence Analysis ({len(results)} claim(s) found)\n"]
    for r in results:
        claim_label = r.get("claim_label") or r.get("node_label", "unknown")
        supported   = not r.get("unsupported", False)
        paths       = r.get("paths", [])

        status = "✓ SUPPORTED" if supported else "✗ UNSUPPORTED"
        lines.append(f"Claim: {claim_label} [{status}]")

        if paths:
            lines.append(f"  Evidence paths ({len(paths)}):")
            for path in paths[:3]:  # cap at 3 paths per claim
                hop_labels = [
                    step.get("node_label") or step.get("edge_type", "?")
                    for step in (path.get("path") or [])
                ]
                lines.append(f"    → {' → '.join(hop_labels)}")
        else:
            lines.append("  No evidence paths found in knowledge graph.")
        lines.append("")

    return "\n".join(lines)


@tool
def get_timeline(
    party_filter: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Build a chronological timeline of events in the case from the knowledge graph.

    Useful for understanding the sequence of events, especially for cases where
    timing matters (breach dates, filing deadlines, notice periods).

    Args:
        party_filter: Optional party name to filter events involving that party
                      (e.g. "Apple", "Epic Games").
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    try:
        nodes, edges = _fetch_kg_graph(case_id)
    except Exception as e:
        return f"ERROR: Could not fetch KG data — {e}"

    if not nodes:
        return "No knowledge graph nodes found for this case."

    try:
        timeline = build_timeline(nodes, edges, party_filter=party_filter)
    except Exception as e:
        return f"ERROR: Timeline build failed — {e}"

    if not timeline:
        return "No timeline events found in the knowledge graph."

    lines = [f"Case Timeline ({len(timeline)} event(s))\n"]
    for event in timeline:
        date_str  = event.get("date") or event.get("date_sort_key") or "Unknown date"
        label     = event.get("node_label") or "Event"
        parties   = event.get("parties") or []
        doc       = event.get("source_document") or ""

        party_str = f" — parties: {', '.join(parties)}" if parties else ""
        doc_str   = f" [{doc}]" if doc else ""
        lines.append(f"• {date_str}: {label}{party_str}{doc_str}")

    return "\n".join(lines)


@tool
def query_kg(
    node_type: str | None = None,
    edge_type: str | None = None,
    node_label_contains: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Traverse the knowledge graph to find entity relationships across documents.

    Use this to find how parties, claims, obligations, and evidence relate to
    each other. Returns node-edge-node triples.

    Args:
        node_type: Filter nodes by type. Options: party, claim, obligation, evidence,
                   event, legal_authority, date, amount, condition.
        edge_type: Filter edges by type. Options: alleged_by, alleged_against,
                   supported_by, obligated_to, breached_by, exhibit_of, same_as,
                   involved_in, quantifies, conditioned_on.
        node_label_contains: Substring filter on node label (e.g. "Apple", "Fortnite").
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    try:
        sb = _get_supabase()
    except Exception as e:
        return f"ERROR: Could not connect to database — {e}"

    # Fetch matching nodes
    node_query = sb.table("kg_nodes").select(
        "id, node_type, node_label, properties, document_id"
    ).eq("case_id", case_id)

    if node_type:
        node_query = node_query.eq("node_type", node_type)
    if node_label_contains:
        node_query = node_query.ilike("node_label", f"%{node_label_contains}%")

    nodes_resp  = node_query.limit(50).execute()
    nodes       = nodes_resp.data or []

    if not nodes:
        return "No matching nodes found in the knowledge graph."

    node_ids   = [n["id"] for n in nodes]
    id_to_node = {n["id"]: n for n in nodes}

    # Fetch edges where source is one of our nodes (kg_edges has no case_id column)
    edges_query = sb.table("kg_edges").select(
        "source_node_id, target_node_id, edge_type, confidence"
    ).in_("source_node_id", node_ids)

    if edge_type:
        edges_query = edges_query.eq("edge_type", edge_type)

    edges_resp = edges_query.limit(200).execute()
    edges      = edges_resp.data or []

    # Filter to edges where at least one endpoint is in our node set
    relevant_edges = [
        e for e in edges
        if e["source_node_id"] in id_to_node or e["target_node_id"] in id_to_node
    ]

    lines = [f"Knowledge Graph Query — {len(nodes)} node(s), {len(relevant_edges)} edge(s)\n"]

    if not relevant_edges:
        # Just list nodes
        lines.append("Nodes:")
        for n in nodes[:20]:
            props = n.get("properties") or {}
            detail = ""
            if n["node_type"] == "party":
                detail = f" (role: {props.get('role', '?')})"
            elif n["node_type"] == "claim":
                detail = f" (type: {props.get('claim_type', '?')})"
            lines.append(f"  [{n['node_type']}] {n['node_label']}{detail}")
    else:
        # Show triples
        lines.append("Relationships:")
        shown = set()
        for e in relevant_edges[:30]:
            src = id_to_node.get(e["source_node_id"])
            tgt = id_to_node.get(e["target_node_id"])
            src_label = src["node_label"] if src else e["source_node_id"][:8]
            tgt_label = tgt["node_label"] if tgt else e["target_node_id"][:8]
            key = (src_label, e["edge_type"], tgt_label)
            if key in shown:
                continue
            shown.add(key)
            conf = f" (conf: {e['confidence']:.2f})" if e.get("confidence") else ""
            lines.append(f"  {src_label} --[{e['edge_type']}]--> {tgt_label}{conf}")

    return "\n".join(lines)
