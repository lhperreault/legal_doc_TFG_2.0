"""
04B_kg_cross_build.py — Phase 2, Step 4B: Cross-Document Knowledge Graph Builder

Runs entity resolution across all documents in a case, merges duplicate
party/evidence/legal_authority nodes, and creates cross-document relationship
edges. Operates on the kg_nodes and kg_edges tables already populated by 04A.

No AI calls — pure Python using fuzzy string matching (difflib).

Usage:
    python 04B_kg_cross_build.py --case_id "uuid-of-case"

Must be run AFTER all documents in the case have been processed through 04A.
This script is NOT called by main.py — invoke it manually or from a future
case-level orchestrator.
"""

import argparse
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Similarity thresholds for cross-document entity resolution
PARTY_RESOLUTION_THRESHOLD     = 0.85
AUTHORITY_RESOLUTION_THRESHOLD = 0.85

# Cross-document edge base confidence; boosted if resolution score > 0.95
CROSS_DOC_CONFIDENCE_BASE   = 0.7
CROSS_DOC_CONFIDENCE_STRONG = 0.8
STRONG_MATCH_THRESHOLD      = 0.95

# Suffixes stripped for party name normalization
_PARTY_SUFFIX_RE = re.compile(
    r',?\s+(?:Inc\.|LLC|Corp\.|Ltd\.|L\.P\.|LLP|P\.C\.|N\.A\.|Co\.|Corporation|Incorporated|Limited)\.?$',
    re.IGNORECASE,
)


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


# ---------------------------------------------------------------------------
# Name normalization and similarity
# ---------------------------------------------------------------------------

def _normalize_party(name: str) -> str:
    n = _PARTY_SUFFIX_RE.sub("", name.strip()).strip().lower()
    return re.sub(r'\s+', ' ', n)


def _normalize_authority(name: str) -> str:
    """Normalize case citation name: lowercase, strip docket-style suffixes."""
    n = name.strip().lower()
    # Strip trailing citation info like "558 U.S. 100" if appended
    n = re.sub(r',?\s+\d+\s+\S+\s+\d+.*$', '', n).strip()
    return re.sub(r'\s+', ' ', n)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _is_substring(a: str, b: str) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False
    return a in b or b in a


def _same_role(node_a: dict, node_b: dict) -> bool:
    ra = ((node_a.get("properties") or {}).get("role") or "").lower()
    rb = ((node_b.get("properties") or {}).get("role") or "").lower()
    return (not ra) or (not rb) or (ra == rb)


# ---------------------------------------------------------------------------
# Document type classification
# ---------------------------------------------------------------------------

def _classify_doc(document_type: str) -> str:
    """Map a document_type string to a routing category."""
    dt = (document_type or "").lower()
    if any(x in dt for x in ("contract", "agreement", "nda", "license", "lease", "purchase")):
        return "contract"
    if any(x in dt for x in ("complaint", "pleading", "petition", "counterclaim")):
        return "complaint"
    if any(x in dt for x in ("appeal", "brief", "motion", "opposition", "reply brief")):
        return "brief"
    if any(x in dt for x in ("order", "opinion", "judgment", "ruling", "decision")):
        return "court_order"
    if any(x in dt for x in ("answer", "response", "admission", "denial")):
        return "answer"
    return "other"


# ---------------------------------------------------------------------------
# Union-Find for cluster building
# ---------------------------------------------------------------------------

class _UnionFind:
    """Minimal union-find for grouping merge candidates into clusters."""

    def __init__(self):
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(a)] = self.find(b)

    def clusters(self, node_ids: list[str]) -> dict[str, list[str]]:
        """Return {canonical_id: [member_ids...]} for all given node_ids."""
        groups: dict[str, list[str]] = defaultdict(list)
        for nid in node_ids:
            groups[self.find(nid)].append(nid)
        return dict(groups)


# ---------------------------------------------------------------------------
# Pass 1: Entity resolution helpers
# ---------------------------------------------------------------------------

def _resolve_parties(
    nodes_by_doc: dict[str, list[dict]],
    merge_log: list[dict],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Cross-document party resolution.
    Returns:
        canonical_map   — node_id → canonical_node_id (only for non-canonical nodes)
        aliases_map     — canonical_node_id → [alias labels]
    """
    uf = _UnionFind()
    score_cache: dict[tuple[str, str], float] = {}

    doc_ids = list(nodes_by_doc.keys())
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            nodes_a = nodes_by_doc[doc_ids[i]]
            nodes_b = nodes_by_doc[doc_ids[j]]
            for na in nodes_a:
                norm_a = _normalize_party(na["node_label"])
                for nb in nodes_b:
                    norm_b = _normalize_party(nb["node_label"])
                    score  = _similarity(norm_a, norm_b)
                    is_sub = _is_substring(norm_a, norm_b) and _same_role(na, nb)

                    if score >= PARTY_RESOLUTION_THRESHOLD or is_sub:
                        uf.union(na["id"], nb["id"])
                        key = (min(na["id"], nb["id"]), max(na["id"], nb["id"]))
                        score_cache[key] = max(score_cache.get(key, 0.0), score)
                        merge_log.append({
                            "type":     "party",
                            "node_a":   na["node_label"],
                            "node_b":   nb["node_label"],
                            "score":    round(score, 3),
                            "method":   "substring" if (is_sub and score < PARTY_RESOLUTION_THRESHOLD) else "similarity",
                            "doc_a":    na["document_id"],
                            "doc_b":    nb["document_id"],
                        })

    all_party_nodes = [n for nodes in nodes_by_doc.values() for n in nodes]
    all_ids = [n["id"] for n in all_party_nodes]
    id_to_node = {n["id"]: n for n in all_party_nodes}

    return _build_canonical_maps(uf, all_ids, id_to_node, score_cache)


def _resolve_evidence(
    nodes_by_doc: dict[str, list[dict]],
    merge_log: list[dict],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Cross-document evidence resolution — exact label match.
    """
    uf = _UnionFind()

    doc_ids = list(nodes_by_doc.keys())
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            for na in nodes_by_doc[doc_ids[i]]:
                for nb in nodes_by_doc[doc_ids[j]]:
                    if na["node_label"].strip().lower() == nb["node_label"].strip().lower():
                        uf.union(na["id"], nb["id"])
                        merge_log.append({
                            "type":   "evidence",
                            "node_a": na["node_label"],
                            "node_b": nb["node_label"],
                            "score":  1.0,
                            "method": "exact",
                            "doc_a":  na["document_id"],
                            "doc_b":  nb["document_id"],
                        })

    all_nodes = [n for nodes in nodes_by_doc.values() for n in nodes]
    all_ids   = [n["id"] for n in all_nodes]
    id_to_node = {n["id"]: n for n in all_nodes}
    return _build_canonical_maps(uf, all_ids, id_to_node, {})


def _resolve_authorities(
    nodes_by_doc: dict[str, list[dict]],
    merge_log: list[dict],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Cross-document legal authority resolution — normalized case name similarity.
    """
    uf = _UnionFind()
    score_cache: dict[tuple[str, str], float] = {}

    doc_ids = list(nodes_by_doc.keys())
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            for na in nodes_by_doc[doc_ids[i]]:
                for nb in nodes_by_doc[doc_ids[j]]:
                    norm_a = _normalize_authority(na["node_label"])
                    norm_b = _normalize_authority(nb["node_label"])
                    score  = _similarity(norm_a, norm_b)
                    if score >= AUTHORITY_RESOLUTION_THRESHOLD:
                        uf.union(na["id"], nb["id"])
                        key = (min(na["id"], nb["id"]), max(na["id"], nb["id"]))
                        score_cache[key] = max(score_cache.get(key, 0.0), score)
                        merge_log.append({
                            "type":   "legal_authority",
                            "node_a": na["node_label"],
                            "node_b": nb["node_label"],
                            "score":  round(score, 3),
                            "method": "similarity",
                            "doc_a":  na["document_id"],
                            "doc_b":  nb["document_id"],
                        })

    all_nodes  = [n for nodes in nodes_by_doc.values() for n in nodes]
    all_ids    = [n["id"] for n in all_nodes]
    id_to_node = {n["id"]: n for n in all_nodes}
    return _build_canonical_maps(uf, all_ids, id_to_node, score_cache)


def _build_canonical_maps(
    uf: _UnionFind,
    all_ids: list[str],
    id_to_node: dict[str, dict],
    score_cache: dict,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    From union-find clusters, elect a canonical node per cluster (longest label),
    and build canonical_map + aliases_map.
    """
    canonical_map: dict[str, str] = {}    # non-canonical node_id → canonical_node_id
    aliases_map:   dict[str, list[str]] = {}  # canonical_node_id → [alias labels]

    clusters = uf.clusters(all_ids)
    for root, members in clusters.items():
        if len(members) < 2:
            continue  # singleton — no resolution needed

        # Elect canonical = node with longest node_label
        canonical_id = max(
            members,
            key=lambda nid: len(id_to_node[nid]["node_label"]) if nid in id_to_node else 0,
        )

        for member_id in members:
            if member_id == canonical_id:
                continue
            canonical_map[member_id] = canonical_id
            node = id_to_node.get(member_id)
            if node:
                aliases_map.setdefault(canonical_id, [])
                lbl = node["node_label"]
                if lbl not in aliases_map[canonical_id]:
                    aliases_map[canonical_id].append(lbl)

    return canonical_map, aliases_map


def _follow_canonical(node_id: str, canonical_map: dict[str, str]) -> str:
    """Follow canonical_node_id chain to the root (with cycle protection)."""
    seen: set[str] = set()
    current = node_id
    while current in canonical_map:
        if current in seen:
            break
        seen.add(current)
        current = canonical_map[current]
    return current


def _build_same_as_edges(
    canonical_map: dict[str, str],
    id_to_node: dict[str, dict],
    document_id_for_edge: str | None = None,
) -> list[dict]:
    """Create same_as cross-document edges for all merged pairs."""
    edges = []
    for non_canonical_id, canonical_id in canonical_map.items():
        node = id_to_node.get(non_canonical_id)
        sec_id = node.get("source_section_id") if node else None
        doc_id = node.get("document_id") if node else document_id_for_edge
        edges.append({
            "id":                 str(uuid.uuid4()),
            "source_node_id":     non_canonical_id,
            "target_node_id":     canonical_id,
            "edge_type":          "same_as",
            "edge_scope":         "cross_document",
            "properties":         {},
            "confidence":         CROSS_DOC_CONFIDENCE_STRONG,
            "source_section_id":  sec_id,
            "source_document_id": doc_id,
        })
    return edges


# ---------------------------------------------------------------------------
# Pass 2: Cross-document edge creation
# ---------------------------------------------------------------------------

def _find_party_canonical(
    name: str,
    party_nodes: list[dict],
    canonical_map: dict[str, str],
    threshold: float = 0.80,
) -> str | None:
    """Fuzzy-match a name to a party node and return its canonical ID."""
    if not name or not party_nodes:
        return None
    norm_target = _normalize_party(name)
    best_id, best_score = None, 0.0
    for node in party_nodes:
        norm_label = _normalize_party(node["node_label"])
        score = _similarity(norm_target, norm_label)
        if _is_substring(norm_target, norm_label):
            score = max(score, threshold)
        if score > best_score:
            best_score, best_id = score, node["id"]
    if best_score >= threshold and best_id:
        return _follow_canonical(best_id, canonical_map)
    return None


def _build_cross_edges(
    all_nodes:      list[dict],
    doc_category:   dict[str, str],   # document_id → category
    section_labels: dict[str, str],   # section_id → semantic_label
    canonical_map:  dict[str, str],
    case_id:        str,
) -> list[dict]:
    """Build all three types of cross-document relationship edges."""
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def _add(source_id: str, target_id: str, edge_type: str,
             sec_id: str | None, doc_id: str, confidence: float) -> None:
        key = (source_id, target_id, edge_type)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "id":                 str(uuid.uuid4()),
            "source_node_id":     source_id,
            "target_node_id":     target_id,
            "edge_type":          edge_type,
            "edge_scope":         "cross_document",
            "properties":         {},
            "confidence":         round(confidence, 3),
            "source_section_id":  sec_id,
            "source_document_id": doc_id,
        })

    # Group nodes by document category and node type
    cat_nodes: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for node in all_nodes:
        doc_id   = node["document_id"]
        cat      = doc_category.get(doc_id, "other")
        ntype    = node["node_type"]
        cat_nodes[cat][ntype].append(node)

    all_party_nodes = [n for n in all_nodes if n["node_type"] == "party"]

    # ------------------------------------------------------------------
    # Rule 1: Contract obligation → breached_by → Complaint claim
    # ------------------------------------------------------------------
    contract_obligations = cat_nodes.get("contract", {}).get("obligation", [])
    complaint_claims     = cat_nodes.get("complaint", {}).get("claim", [])

    for obl in contract_obligations:
        obl_props    = obl.get("properties") or {}
        obligated    = obl_props.get("obligated_party") or ""
        if not obligated:
            continue
        obl_canonical = _find_party_canonical(obligated, all_party_nodes, canonical_map)

        for claim in complaint_claims:
            claim_props = claim.get("properties") or {}
            claim_type  = (claim_props.get("claim_type") or "").lower()
            if "breach" not in claim_type:
                continue
            defendant  = claim_props.get("defendant") or ""
            def_canonical = _find_party_canonical(defendant, all_party_nodes, canonical_map)
            if obl_canonical and def_canonical and obl_canonical == def_canonical:
                _add(
                    obl["id"], claim["id"], "breached_by",
                    obl.get("source_section_id"), obl["document_id"],
                    CROSS_DOC_CONFIDENCE_BASE,
                )

    # ------------------------------------------------------------------
    # Rule 2: Brief argument → challenges → Court order holding
    # ------------------------------------------------------------------
    brief_claims = [
        n for n in cat_nodes.get("brief", {}).get("claim", [])
        if "argument" in (section_labels.get(n.get("source_section_id") or "") or "")
    ]
    order_claims = [
        n for n in cat_nodes.get("court_order", {}).get("claim", [])
        if any(
            kw in (section_labels.get(n.get("source_section_id") or "") or "")
            for kw in ("holding", "order", "ruling", "finding")
        )
    ]

    if brief_claims and order_claims:
        # Find canonical party sets for each document
        def _canonical_parties(nodes: list[dict]) -> set[str]:
            s = set()
            for n in nodes:
                sec_id = n.get("source_section_id")
                if not sec_id:
                    continue
                for p in all_party_nodes:
                    if p.get("source_section_id") == sec_id:
                        s.add(_follow_canonical(p["id"], canonical_map))
            return s

        for arg_node in brief_claims:
            arg_parties = _canonical_parties([arg_node])
            for hold_node in order_claims:
                hold_parties = _canonical_parties([hold_node])
                if arg_parties & hold_parties:   # share at least one canonical party
                    _add(
                        arg_node["id"], hold_node["id"], "challenges",
                        arg_node.get("source_section_id"), arg_node["document_id"],
                        CROSS_DOC_CONFIDENCE_BASE,
                    )

    # ------------------------------------------------------------------
    # Rule 3: Answer admissions_denials → responds_to → Complaint claims
    # ------------------------------------------------------------------
    answer_claims = [
        n for n in cat_nodes.get("answer", {}).get("claim", [])
        if "admission" in (section_labels.get(n.get("source_section_id") or "") or "")
        or "denial"    in (section_labels.get(n.get("source_section_id") or "") or "")
    ]

    if answer_claims and complaint_claims:
        # Match by shared canonical defendant/plaintiff parties
        def _canonical_defendant(node: dict) -> str | None:
            props = node.get("properties") or {}
            name  = props.get("defendant") or props.get("plaintiff") or ""
            return _find_party_canonical(name, all_party_nodes, canonical_map) if name else None

        for ans_claim in answer_claims:
            ans_canon = _canonical_defendant(ans_claim)
            for comp_claim in complaint_claims:
                comp_canon = _canonical_defendant(comp_claim)
                if ans_canon and comp_canon and ans_canon == comp_canon:
                    _add(
                        ans_claim["id"], comp_claim["id"], "responds_to",
                        ans_claim.get("source_section_id"), ans_claim["document_id"],
                        CROSS_DOC_CONFIDENCE_BASE,
                    )

    return edges


# ---------------------------------------------------------------------------
# Batch write helpers
# ---------------------------------------------------------------------------

def _batch_update_canonical(supabase, canonical_map: dict[str, str]) -> int:
    """Update canonical_node_id on all non-canonical nodes. Returns error count."""
    errors = 0
    items = list(canonical_map.items())
    for i in range(0, len(items), 100):
        batch = items[i:i + 100]
        for node_id, canonical_id in batch:
            try:
                supabase.table("kg_nodes").update(
                    {"canonical_node_id": canonical_id}
                ).eq("id", node_id).execute()
            except Exception as e:
                print(f"  WARNING: Could not set canonical_node_id on {node_id} — {e}")
                errors += 1
    return errors


def _batch_update_aliases(
    supabase,
    aliases_map: dict[str, list[str]],
    id_to_node: dict[str, dict],
) -> int:
    """Merge aliases into each canonical node's properties. Returns error count."""
    errors = 0
    for canonical_id, new_aliases in aliases_map.items():
        node = id_to_node.get(canonical_id)
        if not node:
            continue
        props = dict(node.get("properties") or {})
        existing = props.get("aliases") or []
        merged = list({*existing, *new_aliases})
        props["aliases"] = merged
        try:
            supabase.table("kg_nodes").update({"properties": props}).eq("id", canonical_id).execute()
        except Exception as e:
            print(f"  WARNING: Could not update aliases on {canonical_id} — {e}")
            errors += 1
    return errors


def _batch_insert_edges(supabase, edges: list[dict]) -> int:
    """Batch-insert kg_edges in chunks of 100. Returns error count."""
    errors = 0
    for i in range(0, len(edges), 100):
        try:
            supabase.table("kg_edges").insert(edges[i:i + 100]).execute()
        except Exception as e:
            print(f"  WARNING: Edge batch {i // 100 + 1} insert failed — {e}")
            errors += 1
    return errors


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def _write_output_files(
    temp_dir:    str,
    case_id:     str,
    merge_log:   list[dict],
    cross_edges: list[dict],
    canonical_map: dict[str, str],
    node_counts: Counter,
    edge_counts: Counter,
) -> None:
    os.makedirs(temp_dir, exist_ok=True)
    prefix = os.path.join(temp_dir, f"case_{case_id}_04B")

    with open(f"{prefix}_entity_resolution.json", "w", encoding="utf-8") as f:
        json.dump(merge_log, f, indent=2, default=str)

    with open(f"{prefix}_cross_edges.json", "w", encoding="utf-8") as f:
        json.dump(cross_edges, f, indent=2, default=str)

    lines = [
        f"# Cross-Document KG Summary — Case {case_id}",
        f"\n## Entity Resolution ({len(canonical_map)} nodes merged)\n",
        *[f"- **{t}**: {c}" for t, c in Counter(m["type"] for m in merge_log).items()],
        f"\n## Cross-Document Edges ({len(cross_edges)} total)\n",
        *[f"- **{et}**: {cnt}" for et, cnt in sorted(edge_counts.items())],
        f"\n## Nodes Processed by Type\n",
        *[f"- **{nt}**: {cnt}" for nt, cnt in sorted(node_counts.items())],
    ]
    with open(f"{prefix}_summary.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build cross-document knowledge graph for a case."
    )
    parser.add_argument("--case_id", required=True, help="UUID of the case in Supabase")
    args = parser.parse_args()
    case_id = args.case_id

    supabase = _get_supabase()

    # ------------------------------------------------------------------
    # Fetch all data for the case
    # ------------------------------------------------------------------
    print(f"  Fetching KG nodes for case {case_id}...")
    try:
        nodes_resp = (
            supabase.table("kg_nodes")
            .select("id, document_id, case_id, node_type, node_label, properties, source_section_id, source_extraction_id")
            .eq("case_id", case_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch kg_nodes — {e}")
        sys.exit(1)

    all_nodes = nodes_resp.data or []
    if not all_nodes:
        print(f"SUCCESS: No KG nodes found for case {case_id}. 0 merges, 0 cross-edges.")
        return

    print(f"  {len(all_nodes)} nodes loaded")

    # Fetch documents for this case
    doc_ids = list({n["document_id"] for n in all_nodes})
    try:
        docs_resp = (
            supabase.table("documents")
            .select("id, file_name, document_type")
            .in_("id", doc_ids)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch documents — {e}")
        sys.exit(1)

    doc_type_map: dict[str, str] = {
        d["id"]: d.get("document_type") or "" for d in (docs_resp.data or [])
    }
    doc_category: dict[str, str] = {
        doc_id: _classify_doc(dt) for doc_id, dt in doc_type_map.items()
    }

    # Fetch section semantic_labels for nodes that have source_section_id
    section_ids = list({n["source_section_id"] for n in all_nodes if n.get("source_section_id")})
    section_labels: dict[str, str] = {}
    if section_ids:
        try:
            secs_resp = (
                supabase.table("sections")
                .select("id, semantic_label")
                .in_("id", section_ids)
                .execute()
            )
            section_labels = {
                s["id"]: (s.get("semantic_label") or "") for s in (secs_resp.data or [])
            }
        except Exception as e:
            print(f"  WARNING: Could not fetch section labels — {e}")

    print(
        f"  {len(doc_ids)} documents, "
        f"categories: {dict(Counter(doc_category.values()))}"
    )

    # Build global lookups
    id_to_node: dict[str, dict] = {n["id"]: n for n in all_nodes}
    node_counts = Counter(n["node_type"] for n in all_nodes)

    # Group nodes by type, then by document_id
    def _by_doc(node_type: str) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for n in all_nodes:
            if n["node_type"] == node_type:
                groups[n["document_id"]].append(n)
        return dict(groups)

    # ------------------------------------------------------------------
    # Pass 1: Entity Resolution
    # ------------------------------------------------------------------
    print("  Pass 1 — Entity resolution...")
    merge_log: list[dict] = []
    all_same_as_edges: list[dict] = []
    master_canonical: dict[str, str] = {}    # full map: non-canonical → canonical
    master_aliases:   dict[str, list[str]] = {}

    def _apply_resolution(c_map: dict[str, str], a_map: dict[str, list[str]]) -> None:
        master_canonical.update(c_map)
        for cid, aliases in a_map.items():
            master_aliases.setdefault(cid, []).extend(
                a for a in aliases if a not in master_aliases.get(cid, [])
            )
        master_aliases.update(a_map)
        all_same_as_edges.extend(_build_same_as_edges(c_map, id_to_node))

    # Parties
    c_map, a_map = _resolve_parties(_by_doc("party"), merge_log)
    _apply_resolution(c_map, a_map)

    # Evidence
    c_map, a_map = _resolve_evidence(_by_doc("evidence"), merge_log)
    _apply_resolution(c_map, a_map)

    # Legal authorities
    c_map, a_map = _resolve_authorities(_by_doc("legal_authority"), merge_log)
    _apply_resolution(c_map, a_map)

    print(
        f"  Pass 1 — {len(master_canonical)} nodes resolved, "
        f"{len(all_same_as_edges)} same_as edges"
    )

    # ------------------------------------------------------------------
    # Pass 2: Cross-document relationship edges
    # ------------------------------------------------------------------
    print("  Pass 2 — Cross-document edges...")
    cross_edges = _build_cross_edges(
        all_nodes, doc_category, section_labels, master_canonical, case_id
    )
    print(f"  Pass 2 — {len(cross_edges)} cross-document edges")

    all_new_edges = all_same_as_edges + cross_edges

    # ------------------------------------------------------------------
    # Pass 3: Write to Supabase + output files
    # ------------------------------------------------------------------
    print("  Pass 3 — Writing to Supabase...")

    update_errors  = _batch_update_canonical(supabase, master_canonical)
    alias_errors   = _batch_update_aliases(supabase, master_aliases, id_to_node)
    edge_errors    = _batch_insert_edges(supabase, all_new_edges)

    temp_dir = os.path.join(os.path.dirname(__file__), '..', 'zz_temp_chunks')
    edge_counts = Counter(e["edge_type"] for e in all_new_edges)
    _write_output_files(
        temp_dir, case_id, merge_log, all_new_edges,
        master_canonical, node_counts, edge_counts,
    )

    total_errors = update_errors + alias_errors + edge_errors
    if total_errors:
        print(
            f"ERROR: Cross-document KG built with {total_errors} write error(s) for case {case_id}. "
            f"{len(master_canonical)} merges, {len(all_new_edges)} cross-edges."
        )
        sys.exit(1)

    print(
        f"SUCCESS: Cross-document KG built for case {case_id}. "
        f"{len(master_canonical)} merges, {len(all_new_edges)} cross-edges "
        f"[{', '.join(f'{k}:{v}' for k, v in sorted(edge_counts.items()))}]."
    )


if __name__ == "__main__":
    main()
