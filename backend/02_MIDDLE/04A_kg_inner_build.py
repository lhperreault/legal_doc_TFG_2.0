"""
04A_kg_inner_build.py — Phase 2, Step 4A: Intra-Document Knowledge Graph Builder

Reads from the extractions table for a single document.
Creates kg_nodes and kg_edges with edge_scope='intra_document'.
No AI calls — pure Python transformation logic.

Usage:
    python 04A_kg_inner_build.py --file_name "Complaint (Epic Games to Apple"
    python 04A_kg_inner_build.py --document_id "abc-123-uuid"
"""

import argparse
import json
import os
import re
import sys
import uuid
from collections import Counter
from difflib import SequenceMatcher

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extraction type → KG node type mapping
EXTRACTION_TO_NODE_TYPE: dict[str, str] = {
    "party":           "party",
    "claim":           "claim",
    "cause_of_action": "claim",
    "obligation":      "obligation",
    "date":            "event",        # may become procedural_event — see fusion logic
    "evidence_ref":    "evidence",
    "case_citation":   "legal_authority",
    "amount":          "amount",
    "condition":       "condition",
}

# Date types that produce procedural_event nodes instead of event nodes
PROCEDURAL_DATE_TYPES = {"filing", "hearing", "ruling", "motion", "order", "deadline", "service"}

# Date entity_name values that indicate pure metadata (not worth an event node)
METADATA_DATE_NAMES = {
    "date", "filing date", "effective date", "execution date", "contract date",
    "agreement date", "document date", "signing date", "service date",
}

# Date types that are citation/reference noise — never become event nodes
SKIP_DATE_TYPES = {"publication_date", "access_date", "data_period", "citation_date"}

# Regex to match bare ISO dates: "2021-04-15"
_BARE_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Suffixes stripped for party name normalization and deduplication
_PARTY_SUFFIX_RE = re.compile(
    r',?\s+(?:Inc\.|LLC|Corp\.|Ltd\.|L\.P\.|LLP|P\.C\.|N\.A\.|Co\.|Corporation|Incorporated|Limited)\.?$',
    re.IGNORECASE,
)

# Fuzzy match thresholds
PARTY_MATCH_THRESHOLD    = 0.80
EVIDENCE_MATCH_THRESHOLD = 0.90


# ---------------------------------------------------------------------------
# Document category helper
# ---------------------------------------------------------------------------

def _document_category(document_type: str | None) -> str:
    """Map document_type string to a routing category used to gate edge rules."""
    dt = (document_type or "").strip()
    if dt.startswith("Contract"):
        return "contract"
    if any(k in dt for k in ("Appeal", "Brief", "Motion", "Memorandum",
                              "Opposition", "Reply Brief", "Petition")):
        return "appeal"
    if dt.startswith("Pleading") or any(k in dt for k in
                                        ("Complaint", "Answer", "Counterclaim")):
        return "complaint"
    if any(k in dt for k in ("Opinion", "Order", "Judgment", "Ruling",
                              "Decision", "Decree")):
        return "court_order"
    if any(k in dt for k in ("Interrogator", "Request for Production",
                              "Subpoena", "Deposition", "Discovery")):
        return "discovery"
    if any(k in dt for k in ("Exhibit", "Attachment", "Schedule", "Appendix")):
        return "exhibit"
    return "other"


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(supabase, args) -> tuple[str, str, str | None, str | None]:
    """Returns (document_id, file_name, document_type, case_id)."""
    if args.document_id:
        resp = (
            supabase.table("documents")
            .select("id, file_name, document_type, case_id")
            .eq("id", args.document_id)
            .execute()
        )
    else:
        resp = (
            supabase.table("documents")
            .select("id, file_name, document_type, case_id")
            .eq("file_name", args.file_name)
            .execute()
        )
    if not resp.data:
        key = args.document_id or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)
    row = resp.data[0]
    return row["id"], row["file_name"], row.get("document_type"), row.get("case_id")


# ---------------------------------------------------------------------------
# Name normalization and fuzzy matching
# ---------------------------------------------------------------------------

def _normalize_name(name: str, node_type: str = "") -> str:
    """Normalize a label for deduplication key comparisons."""
    n = name.strip().lower()
    if node_type == "party":
        n = _PARTY_SUFFIX_RE.sub("", n).strip()
    return re.sub(r'\s+', ' ', n)


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _is_substring_match(norm_a: str, norm_b: str) -> bool:
    """True if one normalized name is a substring of the other (min 3 chars to avoid noise)."""
    if len(norm_a) < 3 or len(norm_b) < 3:
        return False
    return norm_a in norm_b or norm_b in norm_a


def _same_role_category(node_a: dict, node_b: dict) -> bool:
    """
    For party nodes, check if roles are compatible before merging via substring.
    If either node has no role, we allow the merge (don't block on missing data).
    """
    role_a = ((node_a.get("properties") or {}).get("role") or "").lower()
    role_b = ((node_b.get("properties") or {}).get("role") or "").lower()
    if not role_a or not role_b:
        return True
    return role_a == role_b


def _find_party_node(
    name: str,
    party_nodes: list[dict],
    threshold: float = PARTY_MATCH_THRESHOLD,
) -> tuple[dict | None, bool]:
    """
    Match a name against party nodes using fuzzy similarity OR substring containment.
    Returns (best_node_or_None, is_exact).
    """
    if not name or not party_nodes:
        return None, False
    norm_target = _normalize_name(name, "party")
    best_node, best_score = None, 0.0
    for node in party_nodes:
        norm_label = _normalize_name(node["node_label"], "party")
        score = _name_similarity(norm_target, norm_label)
        # Substring match counts as a strong signal (treat as threshold-level hit)
        if _is_substring_match(norm_target, norm_label):
            score = max(score, threshold)
        if score > best_score:
            best_score, best_node = score, node
    if best_score >= threshold:
        return best_node, (best_score >= 0.99)
    return None, False


def _find_evidence_node(ref_label: str, evidence_nodes: list[dict]) -> dict | None:
    """Match an evidence reference label to an evidence node."""
    if not ref_label or not evidence_nodes:
        return None
    norm = ref_label.strip().lower()
    for node in evidence_nodes:
        n = node["node_label"].strip().lower()
        if n == norm or n.startswith(norm) or norm.startswith(n):
            return node
    return None


# ---------------------------------------------------------------------------
# Fuzzy deduplication (between Pass 1 and Pass 2)
# ---------------------------------------------------------------------------

# Similarity thresholds per node type for post-Pass-1 dedup
_FUZZY_DEDUP_THRESHOLDS: dict[str, float] = {
    "party":           0.85,
    "legal_authority": 0.80,
}


def _fuzzy_dedup_nodes(
    nodes: list[dict],
    extraction_to_node: dict[str, str],
) -> int:
    """
    Merge near-duplicate nodes of the same node_type using fuzzy name matching.

    Runs pairwise comparison within 'party' and 'legal_authority' node groups.
    When two nodes exceed the similarity threshold:
      - Winner = longer node_label (more complete name wins).
      - Loser's source_extraction_ids are merged into the winner's list.
      - Loser's label is appended to the winner's properties.aliases list.
      - All extraction_to_node entries pointing at the loser are redirected to the winner.
      - The loser is removed from nodes.

    Modifies nodes and extraction_to_node in-place.
    Returns the number of nodes removed (merged into winners).
    """
    to_remove: set[str] = set()   # IDs of loser nodes

    for node_type, threshold in _FUZZY_DEDUP_THRESHOLDS.items():
        candidates = [
            n for n in nodes
            if n["node_type"] == node_type and n["id"] not in to_remove
        ]

        for i in range(len(candidates)):
            node_a = candidates[i]
            if node_a["id"] in to_remove:
                continue

            for j in range(i + 1, len(candidates)):
                node_b = candidates[j]
                if node_b["id"] in to_remove:
                    continue

                norm_a = _normalize_name(node_a["node_label"], node_type)
                norm_b = _normalize_name(node_b["node_label"], node_type)
                score  = _name_similarity(norm_a, norm_b)

                # Substring containment is a strong merge signal for parties/authorities.
                # For party nodes, also require compatible roles to avoid merging
                # "Apple (plaintiff)" with an unrelated "Apple (defendant)".
                is_sub = _is_substring_match(norm_a, norm_b)
                if node_type == "party":
                    is_sub = is_sub and _same_role_category(node_a, node_b)

                if score < threshold and not is_sub:
                    continue

                # Winner = longer (more complete) label
                winner, loser = (
                    (node_b, node_a)
                    if len(node_b["node_label"]) > len(node_a["node_label"])
                    else (node_a, node_b)
                )

                # Merge loser's extraction IDs into winner
                loser_ids = loser["properties"].get("source_extraction_ids") or []
                winner_ids = winner["properties"].setdefault("source_extraction_ids", [])
                for eid in loser_ids:
                    if eid not in winner_ids:
                        winner_ids.append(eid)

                # Record loser label as an alias on the winner
                aliases = winner["properties"].setdefault("aliases", [])
                if loser["node_label"] not in aliases:
                    aliases.append(loser["node_label"])

                # Redirect all extraction → node mappings from loser to winner
                loser_id  = loser["id"]
                winner_id = winner["id"]
                for ext_id, nid in list(extraction_to_node.items()):
                    if nid == loser_id:
                        extraction_to_node[ext_id] = winner_id

                to_remove.add(loser_id)

    # Remove losers from the nodes list in-place
    nodes[:] = [n for n in nodes if n["id"] not in to_remove]
    return len(to_remove)


# ---------------------------------------------------------------------------
# Date → Event fusion logic
# ---------------------------------------------------------------------------

def _date_to_event_type(extraction: dict) -> str | None:
    """
    Decide what event node type a date extraction should become, or None to skip.
    Returns: 'event', 'procedural_event', or None (skip — pure metadata).
    """
    props       = extraction.get("properties") or {}
    entity_name = (extraction.get("entity_name") or "").strip()
    date_type   = (props.get("date_type") or "").lower()

    # Skip bare ISO date strings or generic metadata labels
    if not entity_name:
        return None
    if entity_name.lower() in METADATA_DATE_NAMES:
        return None
    if _BARE_DATE_RE.match(entity_name):
        return None

    # Skip citation/reference noise date types entirely
    if date_type in SKIP_DATE_TYPES:
        return None

    # Procedural events: filing, hearing, ruling, etc.
    if date_type in PROCEDURAL_DATE_TYPES:
        return "procedural_event"

    return "event"


# ---------------------------------------------------------------------------
# Pass 1: Build nodes
# ---------------------------------------------------------------------------

def _build_nodes(
    extractions: list[dict],
    document_id: str,
    case_id: str | None,
    section_labels: dict[str, str],
    document_type: str | None,
) -> tuple[list[dict], dict[str, str]]:
    """
    Convert extraction rows → kg_node dicts.
    Returns:
        nodes                  — list of node dicts ready for Supabase insert
        extraction_to_node_id  — maps extraction.id → node.id (for edge building)
    """
    nodes: list[dict] = []
    dedup: dict[tuple[str, str], int] = {}       # (node_type, norm_label) → index in nodes
    extraction_to_node: dict[str, str] = {}      # extraction_id → node_id

    for ext in extractions:
        ext_id      = ext["id"]
        ext_type    = ext.get("extraction_type") or ""
        sec_id      = ext.get("section_id")
        conf        = float(ext.get("confidence") or 0.8)
        props       = ext.get("properties") or {}
        entity_name = (ext.get("entity_name") or "").strip()
        entity_value = ext.get("entity_value")

        # Determine node_type
        node_type = EXTRACTION_TO_NODE_TYPE.get(ext_type)
        if node_type is None:
            continue   # skip: statute, court, judge, attorney, legal_concept, generic, etc.

        # Date → event fusion
        if ext_type == "date":
            resolved_type = _date_to_event_type(ext)
            if resolved_type is None:
                continue   # pure metadata date — skip
            node_type = resolved_type

        # Recover label for case_citation if entity_name is missing
        if not entity_name and ext_type == "case_citation":
            entity_name = props.get("case_name") or props.get("citation") or ""
        if not entity_name:
            continue

        # Truncate long labels for claim/obligation nodes
        node_label = entity_name
        if node_type in ("claim", "obligation") and len(node_label) > 120:
            node_label = node_label[:117] + "…"

        # Build type-specific properties
        if node_type == "party":
            node_props = {
                "role":         props.get("role"),
                "entity_type":  entity_value,
                "jurisdiction": props.get("jurisdiction"),
                "address":      props.get("address"),
            }
        elif node_type == "claim":
            node_props = {
                "claim_type":          entity_value,
                "plaintiff":           props.get("plaintiff"),
                "defendant":           props.get("defendant"),
                "alleged_facts":       props.get("alleged_facts") or [],
                "evidence_references": props.get("evidence_references") or [],
                "damages_sought":      props.get("damages_sought"),
            }
        elif node_type == "obligation":
            node_props = {
                "action":            entity_value,
                "obligated_party":   props.get("obligated_party"),
                "beneficiary_party": props.get("beneficiary_party"),
                "deadline":          props.get("deadline"),
                "condition":         props.get("condition"),
                "amount":            props.get("amount"),
            }
        elif node_type in ("event", "procedural_event"):
            node_props = {
                "date_value":      entity_value,
                "date_type":       (props.get("date_type") or "").lower(),
                "is_relative":     props.get("is_relative", False),
                "reference_event": props.get("reference_event"),
            }
        elif node_type == "evidence":
            node_props = {
                "reference_label": entity_name,
                "description":     props.get("description"),
                "context":         props.get("context"),
            }
        elif node_type == "legal_authority":
            node_props = {
                "citation":  props.get("citation") or entity_value,
                "court":     props.get("court"),
                "year":      props.get("year"),
                "relevance": props.get("relevance"),
            }
        elif node_type == "amount":
            node_props = {
                "value":         entity_value,
                "currency":      props.get("currency", "USD"),
                "payer":         props.get("payer"),
                "payee":         props.get("payee"),
                "is_calculated": props.get("is_calculated", False),
            }
        elif node_type == "condition":
            node_props = {
                "condition_type": entity_value,
                "trigger_event":  props.get("trigger_event"),
                "consequence":    props.get("consequence"),
                "affected_party": props.get("affected_party"),
            }
        else:
            node_props = {}

        # Strip None values from properties
        node_props = {k: v for k, v in node_props.items() if v is not None}

        # Deduplication: same node_type + normalized label → merge
        norm_label = _normalize_name(node_label, node_type)
        dedup_key  = (node_type, norm_label)

        if dedup_key in dedup:
            existing = nodes[dedup[dedup_key]]
            existing["properties"].setdefault("source_extraction_ids", []).append(ext_id)
            extraction_to_node[ext_id] = existing["id"]
            continue

        # New node — pre-generate UUID so edges can reference it before Supabase insert
        node_id = str(uuid.uuid4())
        node = {
            "id":                   node_id,
            "document_id":          document_id,
            "case_id":              case_id,
            "node_type":            node_type,
            "node_label":           node_label,
            "properties":           {
                **node_props,
                "source_extraction_ids":  [ext_id],
                "confidence":             conf,
                "source_semantic_label":  section_labels.get(sec_id, "") if sec_id else "",
                "source_document_type":   document_type or "",
            },
            "source_section_id":    sec_id,
            "source_extraction_id": ext_id,
            "canonical_node_id":    None,
        }

        dedup[dedup_key] = len(nodes)
        nodes.append(node)
        extraction_to_node[ext_id] = node_id

    return nodes, extraction_to_node


# ---------------------------------------------------------------------------
# Pass 2: Build edges
# ---------------------------------------------------------------------------

def _build_edges(
    nodes: list[dict],
    extractions: list[dict],
    document_id: str,
    extraction_to_node: dict[str, str],
    doc_category: str,
) -> list[dict]:
    """Build all intra-document edges. Returns list of edge dicts."""
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    # Lookup structures
    nodes_by_type:    dict[str, list[dict]] = {}
    nodes_by_section: dict[str, list[dict]] = {}

    for node in nodes:
        nodes_by_type.setdefault(node["node_type"], []).append(node)
        sec = node.get("source_section_id")
        if sec:
            nodes_by_section.setdefault(sec, []).append(node)

    party_nodes    = nodes_by_type.get("party", [])
    evidence_nodes = nodes_by_type.get("evidence", [])

    def _add_edge(
        source_id:  str,
        target_id:  str,
        edge_type:  str,
        section_id: str | None,
        confidence: float,
    ) -> None:
        key = (source_id, target_id, edge_type)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "id":                 str(uuid.uuid4()),
            "source_node_id":     source_id,
            "target_node_id":     target_id,
            "edge_type":          edge_type,
            "edge_scope":         "intra_document",
            "properties":         {},
            "confidence":         round(max(0.0, confidence), 3),
            "source_section_id":  section_id,
            "source_document_id": document_id,
        })

    _LITIGATION_CATS = ("complaint", "appeal", "court_order")
    _CONTRACT_CATS   = ("contract", "exhibit")

    # ------------------------------------------------------------------
    # Rule 1: Claims → Parties  (alleged_by / alleged_against)
    # Only meaningful when document asserts claims with plaintiff/defendant roles.
    # ------------------------------------------------------------------
    if doc_category in _LITIGATION_CATS:
        for node in nodes_by_type.get("claim", []):
            props  = node.get("properties") or {}
            sec_id = node.get("source_section_id")
            conf   = float(props.get("confidence", 0.8))

            for name, edge_type, direction in [
                (props.get("plaintiff"), "alleged_by",      "party→claim"),
                (props.get("defendant"), "alleged_against", "claim→party"),
            ]:
                if not name:
                    continue
                p_node, is_exact = _find_party_node(name, party_nodes)
                if not p_node:
                    continue
                c = conf if is_exact else conf - 0.1
                if direction == "party→claim":
                    _add_edge(p_node["id"], node["id"], edge_type, sec_id, c)
                else:
                    _add_edge(node["id"], p_node["id"], edge_type, sec_id, c)

    # ------------------------------------------------------------------
    # Rule 2: Claims → Evidence  (supported_by)  — all categories
    # ------------------------------------------------------------------
    for node in nodes_by_type.get("claim", []):
        props  = node.get("properties") or {}
        sec_id = node.get("source_section_id")
        conf   = float(props.get("confidence", 0.8))
        for ref in props.get("evidence_references") or []:
            e_node = _find_evidence_node(ref, evidence_nodes)
            if e_node:
                _add_edge(node["id"], e_node["id"], "supported_by", sec_id, conf - 0.05)

    # ------------------------------------------------------------------
    # Rule 3: Claims → Legal Authority  (relies_on)  — all categories
    # From co-location in the same section.
    # ------------------------------------------------------------------
    for node in nodes_by_type.get("claim", []):
        sec_id = node.get("source_section_id")
        conf   = float((node.get("properties") or {}).get("confidence", 0.8))
        if not sec_id:
            continue
        for sibling in nodes_by_section.get(sec_id, []):
            if sibling["node_type"] == "legal_authority" and sibling["id"] != node["id"]:
                _add_edge(node["id"], sibling["id"], "relies_on", sec_id, conf - 0.05)

    # ------------------------------------------------------------------
    # Rule 4: Obligations → Parties  (obligated_to / beneficiary_of)
    # Only meaningful for contract/exhibit documents with defined duties.
    # ------------------------------------------------------------------
    if doc_category in _CONTRACT_CATS:
        for node in nodes_by_type.get("obligation", []):
            props  = node.get("properties") or {}
            sec_id = node.get("source_section_id")
            conf   = float(props.get("confidence", 0.8))

            for name, edge_type in [
                (props.get("obligated_party"),  "obligated_to"),
                (props.get("beneficiary_party"), "beneficiary_of"),
            ]:
                if not name:
                    continue
                p_node, is_exact = _find_party_node(name, party_nodes)
                if p_node:
                    c = conf if is_exact else conf - 0.1
                    _add_edge(p_node["id"], node["id"], edge_type, sec_id, c)

    # ------------------------------------------------------------------
    # Rule 5: Obligations → Conditions  (conditioned_on)
    # Only meaningful for contract/exhibit documents.
    # From co-location in the same section.
    # ------------------------------------------------------------------
    if doc_category in _CONTRACT_CATS:
        for node in nodes_by_type.get("condition", []):
            sec_id = node.get("source_section_id")
            conf   = float((node.get("properties") or {}).get("confidence", 0.8))
            if not sec_id:
                continue
            for sibling in nodes_by_section.get(sec_id, []):
                if sibling["node_type"] == "obligation" and sibling["id"] != node["id"]:
                    _add_edge(sibling["id"], node["id"], "conditioned_on", sec_id, conf - 0.05)

    # ------------------------------------------------------------------
    # Rule 6: Events → Parties  (involved_in)  — all categories
    # From co-location in the same section.
    # ------------------------------------------------------------------
    event_nodes = (
        nodes_by_type.get("event", []) +
        nodes_by_type.get("procedural_event", [])
    )
    for node in event_nodes:
        sec_id = node.get("source_section_id")
        conf   = float((node.get("properties") or {}).get("confidence", 0.8))
        if not sec_id:
            continue
        for sibling in nodes_by_section.get(sec_id, []):
            if sibling["node_type"] == "party" and sibling["id"] != node["id"]:
                _add_edge(sibling["id"], node["id"], "involved_in", sec_id, conf - 0.1)

    # ------------------------------------------------------------------
    # Rule 7: Amounts → Claims / Parties  (quantifies / damages_sought_by / damages_from)
    # quantifies fires for all categories; damages edges only for litigation docs.
    # ------------------------------------------------------------------
    for node in nodes_by_type.get("amount", []):
        props  = node.get("properties") or {}
        sec_id = node.get("source_section_id")
        conf   = float(props.get("confidence", 0.8))

        # Link to claim nodes in the same section — always
        if sec_id:
            for sibling in nodes_by_section.get(sec_id, []):
                if sibling["node_type"] == "claim" and sibling["id"] != node["id"]:
                    _add_edge(node["id"], sibling["id"], "quantifies", sec_id, conf - 0.05)

        # Link payer → damages_from, payee → damages_sought_by (litigation only)
        if doc_category in _LITIGATION_CATS:
            for name, edge_type in [
                (props.get("payee"), "damages_sought_by"),
                (props.get("payer"), "damages_from"),
            ]:
                if not name:
                    continue
                p_node, is_exact = _find_party_node(name, party_nodes)
                if p_node:
                    c = conf if is_exact else conf - 0.1
                    _add_edge(node["id"], p_node["id"], edge_type, sec_id, c)

    return edges


# ---------------------------------------------------------------------------
# Batch write helpers
# ---------------------------------------------------------------------------

def _write_nodes(supabase, nodes: list[dict]) -> int:
    """Batch-insert kg_nodes in chunks of 100. Returns error count."""
    errors = 0
    for i in range(0, len(nodes), 100):
        batch = nodes[i:i + 100]
        # Omit canonical_node_id (NULL FK — Supabase prefers omitting over explicit null)
        clean = [{k: v for k, v in n.items() if k != "canonical_node_id" or v is not None} for n in batch]
        try:
            supabase.table("kg_nodes").insert(clean).execute()
        except Exception as e:
            print(f"  WARNING: Node batch {i // 100 + 1} insert failed — {e}")
            errors += 1
    return errors


def _write_edges(supabase, edges: list[dict]) -> int:
    """Batch-insert kg_edges in chunks of 100. Returns error count."""
    errors = 0
    for i in range(0, len(edges), 100):
        batch = edges[i:i + 100]
        try:
            supabase.table("kg_edges").insert(batch).execute()
        except Exception as e:
            print(f"  WARNING: Edge batch {i // 100 + 1} insert failed — {e}")
            errors += 1
    return errors


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def _write_output_files(
    temp_dir: str,
    file_name: str,
    nodes: list[dict],
    edges: list[dict],
) -> None:
    os.makedirs(temp_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', '_', file_name)

    with open(os.path.join(temp_dir, f"{safe}_04A_kg_nodes.json"), "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2, default=str)

    with open(os.path.join(temp_dir, f"{safe}_04A_kg_edges.json"), "w", encoding="utf-8") as f:
        json.dump(edges, f, indent=2, default=str)

    node_counts = Counter(n["node_type"] for n in nodes)
    edge_counts = Counter(e["edge_type"] for e in edges)

    lines = [
        f"# KG Summary — {file_name}",
        f"\n## Nodes ({len(nodes)} total)\n",
        *[f"- **{nt}**: {cnt}" for nt, cnt in sorted(node_counts.items())],
        f"\n## Edges ({len(edges)} total)\n",
        *[f"- **{et}**: {cnt}" for et, cnt in sorted(edge_counts.items())],
    ]
    with open(os.path.join(temp_dir, f"{safe}_04A_kg_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build intra-document knowledge graph.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    supabase = _get_supabase()
    document_id, file_name, document_type, case_id = _resolve_document(supabase, args)

    if case_id is None:
        print(
            f"  WARNING: documents.case_id is NULL for '{file_name}'. "
            "Nodes will have case_id=NULL. 04B will skip this document."
        )

    # Fetch all extractions for this document
    try:
        resp = (
            supabase.table("extractions")
            .select(
                "id, section_id, document_id, extraction_type, "
                "entity_name, entity_value, confidence, page_range, properties"
            )
            .eq("document_id", document_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch extractions — {e}")
        sys.exit(1)

    extractions = resp.data or []

    if not extractions:
        print(f"SUCCESS: No extractions found for '{file_name}'. 0 nodes, 0 edges.")
        return

    print(f"  {len(extractions)} extractions loaded for '{file_name}'")

    # Fetch semantic_label for each section referenced by extractions
    section_ids = list({e["section_id"] for e in extractions if e.get("section_id")})
    section_labels: dict[str, str] = {}
    for i in range(0, len(section_ids), 100):
        try:
            s_resp = (
                supabase.table("sections")
                .select("id, semantic_label")
                .in_("id", section_ids[i:i + 100])
                .execute()
            )
            for row in (s_resp.data or []):
                section_labels[row["id"]] = row.get("semantic_label") or ""
        except Exception as e:
            print(f"  WARNING: Could not fetch section labels (batch {i // 100 + 1}) — {e}")

    doc_category = _document_category(document_type)
    print(f"  Document category: '{doc_category}' (type='{document_type or ''}')")

    # Clear existing KG for this document (idempotent re-runs).
    # Edges are cascade-deleted when nodes are deleted.
    try:
        supabase.table("kg_nodes").delete().eq("document_id", document_id).execute()
    except Exception as e:
        print(f"  WARNING: Could not clear existing KG nodes — {e}")

    # --- Pass 1: Nodes ---
    nodes, extraction_to_node = _build_nodes(
        extractions, document_id, case_id, section_labels, document_type
    )
    print(f"  Pass 1 — {len(nodes)} nodes created")

    # --- Fuzzy dedup: merge near-duplicate party / legal_authority nodes ---
    merge_count = _fuzzy_dedup_nodes(nodes, extraction_to_node)
    print(f"  Fuzzy dedup — merged {merge_count} duplicate nodes ({len(nodes)} remaining)")

    # --- Pass 2: Edges ---
    edges = _build_edges(nodes, extractions, document_id, extraction_to_node, doc_category)
    print(f"  Pass 2 — {len(edges)} edges created")

    # --- Pass 3: Write ---
    node_errors = _write_nodes(supabase, nodes)
    edge_errors = _write_edges(supabase, edges)

    temp_dir = os.path.join(os.path.dirname(__file__), '..', 'zz_temp_chunks')
    _write_output_files(temp_dir, file_name, nodes, edges)

    node_counts = Counter(n["node_type"] for n in nodes)
    edge_counts = Counter(e["edge_type"] for e in edges)
    node_summary = ", ".join(f"{k}:{v}" for k, v in sorted(node_counts.items()))
    edge_summary = ", ".join(f"{k}:{v}" for k, v in sorted(edge_counts.items()))

    if node_errors or edge_errors:
        print(
            f"ERROR: KG built with write errors for '{file_name}'. "
            f"{len(nodes)} nodes ({node_errors} batch(es) failed), "
            f"{len(edges)} edges ({edge_errors} batch(es) failed)."
        )
        sys.exit(1)

    print(
        f"SUCCESS: KG built for '{file_name}'. "
        f"{len(nodes)} nodes [{node_summary}], "
        f"{len(edges)} edges [{edge_summary}]."
    )


if __name__ == "__main__":
    main()
