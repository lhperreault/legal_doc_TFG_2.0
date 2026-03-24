# Phase 2, Step 5: Graph Analytics — Planning Document

## For: Claude Code implementation of `05_graph_analytics.py`

**Date:** March 2026
**Location:** `backend/02_MIDDLE/05_graph_analytics.py`

---

## 0. What This Is

A single Python file with **importable functions** and a **CLI wrapper**. The functions query `kg_nodes` and `kg_edges` in Supabase, traverse the graph in memory, and return structured results. The CLI wrapper calls these functions and writes output to `zz_temp_chunks/`.

These are **query-time operations**, not pipeline steps that produce new data. They read from the KG — they don't write to it.

**Two analytics to build:**
1. **Timeline generation** — chronological list of events with provenance
2. **Claim → Evidence path finder** — for each claim, show what evidence supports it

---

## 1. File Structure

```python
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
```

The file has two layers:
- **Core functions** that take lists of node/edge dicts (already fetched) and return structured results. No Supabase dependency — pure Python. This makes them testable and reusable by agents.
- **CLI wrapper** in `main()` that handles Supabase fetching, calls the core functions, and writes output files.

---

## 2. Supabase Data Fetching (CLI layer only)

The CLI wrapper fetches all nodes and edges for the scope (case or document) and passes them to the core functions.

```python
def _fetch_graph(supabase, case_id=None, document_id=None):
    """
    Fetch all kg_nodes and kg_edges for a case or document.
    Returns (nodes: list[dict], edges: list[dict])
    """
    # Fetch nodes
    query = supabase.table("kg_nodes").select("*")
    if case_id:
        query = query.eq("case_id", case_id)
    elif document_id:
        query = query.eq("document_id", document_id)
    nodes = query.execute().data or []

    # Fetch edges — need to join through nodes
    node_ids = [n["id"] for n in nodes]
    edges = []
    # Fetch in batches (PostgREST `in_` has limits)
    for i in range(0, len(node_ids), 100):
        batch = node_ids[i:i+100]
        resp = supabase.table("kg_edges").select("*").in_("source_node_id", batch).execute()
        edges.extend(resp.data or [])

    return nodes, edges
```

Also fetch section data for provenance display:

```python
def _fetch_sections_lookup(supabase, section_ids: list[str]) -> dict[str, dict]:
    """Fetch section titles and page ranges for provenance display."""
    lookup = {}
    for i in range(0, len(section_ids), 100):
        batch = section_ids[i:i+100]
        resp = supabase.table("sections").select("id, section_title, page_range, document_id").in_("id", batch).execute()
        for row in (resp.data or []):
            lookup[row["id"]] = row
    return lookup
```

And document names:

```python
def _fetch_documents_lookup(supabase, document_ids: list[str]) -> dict[str, str]:
    """Fetch document file_names for display."""
    lookup = {}
    for i in range(0, len(document_ids), 100):
        batch = document_ids[i:i+100]
        resp = supabase.table("documents").select("id, file_name").in_("id", batch).execute()
        for row in (resp.data or []):
            lookup[row["id"]] = row["file_name"]
    return lookup
```

---

## 3. Analytics Function 1: Timeline Generation

### Core Function Signature

```python
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
        List of timeline entries sorted by date, each containing:
        {
            "date_value": "2020-08-13",          # ISO date or descriptive string
            "date_sort_key": "2020-08-13",        # for sorting (ISO or "9999" for unresolvable)
            "event_label": "Apple terminates Epic's developer account",
            "event_type": "event" | "procedural_event",
            "node_id": "uuid",
            "involved_parties": ["Epic Games", "Apple Inc."],
            "source_section_id": "uuid",
            "source_document_id": "uuid",
            "confidence": 0.8,
            "is_relative": false,
            "reference_event": null,
            "properties": { ... }                  # full node properties
        }
    """
```

### Implementation Logic

1. Filter `nodes` to those with `node_type` in `("event", "procedural_event")`. If `include_procedural` is False, only keep `"event"`.

2. For each event node, find connected parties by walking edges:
   - Find all edges where `edge_type == "involved_in"` and `target_node_id == event_node_id`
   - Look up the source nodes — those are the parties
   - Store their `node_label` values in `involved_parties`

3. If `party_filter` is set, skip events where no involved party name fuzzy-matches the filter. Use simple `str.lower() in` substring matching — no need for SequenceMatcher here.

4. Extract `date_value` from `node.properties.date_value`. Build a `date_sort_key`:
   - If it's a valid ISO date (YYYY-MM-DD), use it directly
   - If it's just a year ("2020"), use "2020-01-01" for sorting
   - If it's relative or unparseable, use "9999-99-99" (sorts to end)

5. Sort by `date_sort_key` ascending, then by `event_type` (procedural_event before event for same date).

6. Return the list.

### Edge Cases
- Nodes with `is_relative: True` — include them in the timeline but with `date_sort_key = "9999-99-99"` and a note. The frontend/agent can attempt resolution later.
- Nodes with no `date_value` in properties — skip them entirely, log a warning.
- Duplicate events on the same date — keep all of them, don't dedup (different events can happen on the same day).

---

## 4. Analytics Function 2: Claim → Evidence Path Finder

### Core Function Signature

```python
def find_claim_evidence_paths(
    nodes: list[dict],
    edges: list[dict],
    max_hops: int = 3,
    claim_filter: str | None = None,
) -> list[dict]:
    """
    For each claim node, find all paths to evidence nodes.

    Args:
        nodes: all kg_nodes for the scope
        edges: all kg_edges for the scope
        max_hops: maximum edge traversals (default 3 — claim → evidence is usually 1 hop,
                  but claim → legal_authority → evidence or claim → party → evidence may be 2-3)
        claim_filter: if set, only process claims whose label contains this string

    Returns:
        List of claim-evidence bundles:
        {
            "claim_node_id": "uuid",
            "claim_label": "Apple monopolizes the iOS App Distribution Market...",
            "claim_type": "Monopolization",
            "claim_confidence": 0.9,
            "source_section_id": "uuid",
            "source_document_id": "uuid",
            "evidence_paths": [
                {
                    "evidence_node_id": "uuid",
                    "evidence_label": "Exhibit A - Developer Agreement",
                    "evidence_type": "evidence",
                    "path": [
                        {"node_id": "...", "node_label": "...", "node_type": "claim"},
                        {"edge_type": "supported_by", "confidence": 0.85},
                        {"node_id": "...", "node_label": "...", "node_type": "evidence"}
                    ],
                    "hop_count": 1,
                    "path_confidence": 0.85   # minimum confidence along the path
                }
            ],
            "unsupported": true | false       # true if no evidence paths found
        }
    """
```

### Implementation Logic

1. Build adjacency structures from edges:
   ```python
   # Forward adjacency: node_id → [(target_id, edge_type, confidence), ...]
   adj = defaultdict(list)
   for edge in edges:
       adj[edge["source_node_id"]].append((
           edge["target_node_id"],
           edge["edge_type"],
           edge.get("confidence", 0.5)
       ))
       # Also add reverse direction for undirected traversal
       adj[edge["target_node_id"]].append((
           edge["source_node_id"],
           edge["edge_type"] + "_reverse",
           edge.get("confidence", 0.5)
       ))
   ```

2. Build a node lookup: `node_by_id = {n["id"]: n for n in nodes}`

3. Filter nodes to claims: `node_type == "claim"`. If `claim_filter` is set, also filter by label substring match.

4. For each claim node, run BFS up to `max_hops`:
   - Start from the claim node
   - At each hop, follow edges to adjacent nodes
   - If an adjacent node has `node_type == "evidence"` or `node_type == "legal_authority"`, record the path
   - Track visited nodes to avoid cycles
   - Record the full path as alternating node/edge entries (for provenance display)
   - `path_confidence` = minimum confidence of all edges in the path

5. Sort claims by: unsupported first (these need attention), then by number of evidence paths descending.

6. **Important edge types to follow:** `supported_by`, `references`, `relies_on`. Don't follow `alleged_by`, `alleged_against`, `involved_in` — those lead to parties, not evidence. Only follow edges that move toward evidence/authority nodes.

   Actually, refine this: use a whitelist of "evidence-direction" edge types:
   ```python
   EVIDENCE_EDGE_TYPES = {
       "supported_by", "references", "relies_on", "distinguishes",
       # reverse directions (evidence pointing back to claims)
       "supported_by_reverse", "references_reverse",
       "relies_on_reverse", "distinguishes_reverse",
   }
   ```
   Only traverse edges in this set during BFS. This prevents the search from wandering through party nodes and obligation chains.

### Edge Cases
- Claims with no evidence paths — mark as `unsupported: True`. This is valuable information for the lawyer ("these claims have no supporting evidence linked yet").
- Circular paths — track visited set per BFS, skip already-visited nodes.
- Very long paths (3+ hops) — still record them but they're lower value. The `hop_count` field lets the consumer decide what to trust.

---

## 5. CLI Wrapper

```python
def main():
    parser = argparse.ArgumentParser(description="Graph analytics over KG.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case_id", help="UUID of the case")
    group.add_argument("--document_id", help="UUID of a single document")
    parser.add_argument("--mode", required=True,
                        choices=["timeline", "claim_evidence", "all"],
                        help="Which analytics to run")
    parser.add_argument("--party_filter", default=None,
                        help="Filter timeline events by party name")
    parser.add_argument("--claim_filter", default=None,
                        help="Filter claims by label substring")
    args = parser.parse_args()
```

### Output Files

Write to `backend/zz_temp_chunks/`:

**Timeline mode:**
- `{scope}_05_timeline.json` — full structured timeline data
- `{scope}_05_timeline.md` — human-readable markdown:
  ```
  # Case Timeline

  ## 2020-08-13 — Apple terminates Epic's developer account
  **Type:** event | **Confidence:** 0.8
  **Parties:** Epic Games, Apple Inc.
  **Source:** Complaint §47-52 (pages 15-18)

  ## 2020-08-13 — Epic files antitrust complaint
  **Type:** procedural_event | **Confidence:** 0.9
  **Source:** Complaint, full document
  ```

**Claim-evidence mode:**
- `{scope}_05_claim_evidence.json` — full structured path data
- `{scope}_05_claim_evidence.md` — human-readable markdown:
  ```
  # Claim → Evidence Analysis

  ## ⚠️ UNSUPPORTED CLAIMS (no evidence linked)

  ### "Apple has become a market behemoth..."
  - Claim type: Allegation
  - Source: Complaint, Introduction (pages 1-3)
  - **No evidence paths found**

  ## ✅ SUPPORTED CLAIMS

  ### "Apple conditions all developers' access to iOS..."
  - Claim type: Allegation
  - Source: Complaint §34 (pages 12-13)
  - Evidence path (1 hop, confidence: 0.85):
    Claim → supported_by → Exhibit A (Developer Agreement)
  ```

**All mode:** runs both and writes all four files.

### Exit Line

```python
print(f"SUCCESS: Graph analytics complete. Mode: {args.mode}. ...")
```

---

## 6. Dependencies

**Standard library only.** No new pip installs.

- `collections.defaultdict` — for adjacency lists
- `re`, `json`, `os`, `sys`, `argparse` — standard CLI/IO
- `supabase-py` — already installed (only used in CLI wrapper, not in core functions)
- `python-dotenv` — already installed

Do NOT use NetworkX. The graph is small enough (hundreds of nodes) that BFS with a dict adjacency list is simpler and has zero dependency overhead. NetworkX can be added later if graph operations get more complex.

---

## 7. Integration Notes

### Importable Usage (for future agents in Phase 4)

```python
# Agent code can do:
from backend.02_MIDDLE.graph_analytics import build_timeline, find_claim_evidence_paths

# Or if import path is awkward, just sys.path.append and import
import sys
sys.path.append("backend/02_MIDDLE")
from graph_analytics import build_timeline, find_claim_evidence_paths

# Pass pre-fetched nodes/edges (agent already has them from its own Supabase queries)
timeline = build_timeline(nodes, edges, party_filter="Apple")
paths = find_claim_evidence_paths(nodes, edges, claim_filter="monopol")
```

### Not added to main.py

This script is NOT added to the `02_MIDDLE/main.py` orchestrator chain. It's a query-time tool, not a pipeline step. It doesn't produce data that downstream steps depend on. Call it manually or from agents.

---

## 8. Existing Schema Reference (READ ONLY)

### kg_nodes columns
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| document_id | UUID (FK) | |
| case_id | UUID | |
| node_type | TEXT | "party", "claim", "event", "procedural_event", "evidence", "legal_authority", "amount", "obligation", "condition" |
| node_label | TEXT | Display name |
| properties | JSONB | Contains `date_value`, `date_type`, `is_relative`, `confidence`, `source_extraction_ids`, `source_document_type`, `source_semantic_label`, `aliases`, etc. |
| source_section_id | UUID (FK) | |
| source_extraction_id | UUID (FK) | |
| canonical_node_id | UUID (FK) | Set by 04B for merged entities |

### kg_edges columns
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| source_node_id | UUID (FK) | |
| target_node_id | UUID (FK) | |
| edge_type | TEXT | "alleged_by", "supported_by", "relies_on", "involved_in", "obligated_to", etc. |
| edge_scope | TEXT | "intra_document" or "cross_document" |
| properties | JSONB | |
| confidence | FLOAT | |
| source_section_id | UUID (FK) | |
| source_document_id | UUID (FK) | |

---

## 9. File Location

```
backend/02_MIDDLE/
    00_section_refine.py          # existing
    01_AST_tree_build.py          # existing
    02_AST_semantic_label.py      # existing
    03_entity_extraction.py       # existing
    04A_kg_inner_build.py         # existing
    04B_kg_cross_build.py         # existing
    05_graph_analytics.py         # NEW — this file
    main.py                       # NOT modified (05 is not a pipeline step)
```