# Checklist Runner Spec — Two-Tier Template System

## Overview

The checklist runner orchestrates the full case analysis by:
1. Always running the Tier 1 universal template
2. Auto-detecting which Tier 2 add-on modules apply based on case data
3. Executing tasks in dependency order (Layer 1 before Layer 2, universal before add-ons)
4. Dispatching each task to the agent graph as an independent query
5. Collecting results with HITL flagging and provenance
6. Persisting the completed checklist to Supabase

## File Location

`backend/04_AGENTIC_ARCHITECTURE/checklist_runner.py`

## Dependencies

- `graph.py` — the compiled LangGraph agent graph (already built)
- `persistence.py` — saves individual agent responses (already built)
- Template JSON files in `schemas/checklist_templates/`
- `template_registry.json` in `schemas/checklist_templates/`
- Supabase tables: `documents`, `cases`, `extractions`, `agent_responses`

## New Supabase Table

```sql
CREATE TABLE IF NOT EXISTS checklist_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID NOT NULL REFERENCES cases(id),
    templates_used      JSONB NOT NULL,        -- ["universal_commercial", "addon_contract_dispute"]
    total_tasks         INTEGER NOT NULL,
    completed           INTEGER DEFAULT 0,
    failed              INTEGER DEFAULT 0,
    flagged_for_review  INTEGER DEFAULT 0,
    overall_confidence  FLOAT,
    status              TEXT DEFAULT 'running', -- running, completed, failed, partial
    results             JSONB,                  -- full task results array
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_checklist_runs_case ON checklist_runs (case_id);
```

---

## Core Architecture

### Execution Flow

```
run_checklist(case_id)
│
├── 1. Load template registry
│
├── 2. Load Tier 1: universal_commercial.json
│
├── 3. Detect Tier 2 add-ons
│   ├── Fetch case document_types from Supabase
│   ├── Fetch case claim keywords from extractions table
│   ├── For each add-on in registry:
│   │   └── Check detection_rules against case data
│   └── Collect matching add-on template IDs
│
├── 4. Load matching Tier 2 templates
│
├── 5. Merge all tasks into a single execution plan
│   ├── Universal Layer 1 tasks first
│   ├── Universal Layer 2 tasks next (respecting depends_on)
│   └── Add-on tasks last (respecting depends_on_universal)
│
├── 6. Execute tasks in order
│   ├── For each task:
│   │   ├── Check if depends_on tasks completed successfully
│   │   ├── Build context from completed dependency results
│   │   ├── Invoke agent graph with task query + context
│   │   ├── Capture result (answer, confidence, provenance, needs_review)
│   │   └── Store in results map
│   └── Continue even if a task fails (log error, mark as failed)
│
├── 7. Calculate overall metrics
│   ├── Overall confidence = weighted average of task confidences
│   ├── Completion rate = completed / total
│   └── Review count = tasks with needs_review = true
│
├── 8. Persist checklist run to Supabase
│
└── 9. Return structured checklist result
```

### Dependency Resolution

Tasks declare dependencies via `depends_on` (within the same template) or `depends_on_universal` (add-on tasks referencing universal tasks). The runner must execute in topological order.

```python
def _build_execution_order(all_tasks: list[dict]) -> list[dict]:
    """
    Topological sort of tasks based on depends_on and depends_on_universal.
    
    Rules:
    1. Tasks with no dependencies run first
    2. Layer 1 tasks before Layer 2 tasks (within same template)
    3. Universal tasks before add-on tasks
    4. Within the same layer/tier, respect depends_on ordering
    5. If a dependency failed, the dependent task still runs but gets 
       a context note: "Note: dependency task '{id}' failed — results may be incomplete"
    """
    # Implementation: standard topological sort using Kahn's algorithm
    # Group by: (tier, layer, dependency_depth)
    # Tie-break by task order in the JSON (preserves author's intended sequence)
```

### Context Injection from Dependencies

When a task depends on a previously completed task, the runner injects the dependency's answer as context into the query. This prevents the agent from re-doing work and helps it build on prior analysis.

```python
def _build_task_query(task: dict, completed_results: dict[str, dict]) -> str:
    """
    Build the full query for a task, injecting context from completed dependencies.
    
    Example: If L2_4.6_breach_sequencing depends on L1_3.6_timeline and L1_3.9_claims,
    the query becomes:
    
        "CONTEXT FROM PRIOR ANALYSIS:
        
        [Timeline (L1_3.6)]: A chronological timeline has been built...
        [Claims (L1_3.9)]: The complaint alleges three causes of action...
        
        YOUR TASK:
        Analyze the timeline for breach patterns: Who acted first..."
    """
    deps = task.get("depends_on", []) + task.get("depends_on_universal", [])
    
    context_blocks = []
    for dep_id in deps:
        dep_result = completed_results.get(dep_id)
        if dep_result and dep_result.get("status") == "completed":
            label = dep_result.get("task_label", dep_id)
            answer = dep_result.get("answer", "")
            # Truncate long answers to avoid blowing up context
            if len(answer) > 2000:
                answer = answer[:2000] + "\n[... truncated for context ...]"
            context_blocks.append(f"[{label} ({dep_id})]: {answer}")
        elif dep_result and dep_result.get("status") == "failed":
            context_blocks.append(
                f"[{dep_id}]: This prior analysis task failed. "
                "Proceed with available information — results may be incomplete."
            )
    
    if context_blocks:
        context = "CONTEXT FROM PRIOR ANALYSIS:\n\n" + "\n\n".join(context_blocks)
        return f"{context}\n\nYOUR TASK:\n{task['query']}"
    else:
        return task["query"]
```

---

## Add-On Detection Logic

```python
def _detect_addons(
    case_id: str,
    registry: dict,
    supabase,
) -> list[str]:
    """
    Check each Tier 2 add-on's detection_rules against the case data.
    Returns list of matching template IDs.
    """
    # Fetch case document types
    docs_resp = (
        supabase.table("documents")
        .select("document_type")
        .eq("case_id", case_id)
        .execute()
    )
    doc_types = [d["document_type"] or "" for d in (docs_resp.data or [])]
    doc_types_lower = [dt.lower() for dt in doc_types]
    
    # Fetch claim keywords from extractions (claim descriptions + types)
    claims_resp = (
        supabase.table("extractions")
        .select("entity_name, entity_value, properties")
        .eq("extraction_type", "claim")
        .in_(
            "document_id",
            [d["id"] for d in supabase.table("documents")
                .select("id").eq("case_id", case_id).execute().data or []]
        )
        .limit(100)
        .execute()
    )
    claim_text = " ".join(
        (c.get("entity_name") or "") + " " + (c.get("entity_value") or "")
        for c in (claims_resp.data or [])
    ).lower()
    
    # Check multi-jurisdiction (for cross-border detection)
    jurisdictions = set()
    for doc_type in doc_types:
        # Simple heuristic — real implementation would check extracted jurisdictions
        pass  
    
    matching = []
    
    for addon_id, addon_info in registry.get("tier_2", {}).items():
        if addon_info.get("status") != "built":
            continue  # skip planned-but-not-built templates
        
        rules = addon_info.get("detection_rules", {})
        matched = False
        
        # Check doc type rules
        required_doc_types = rules.get("requires_any_doc_type", [])
        if required_doc_types:
            for rdt in required_doc_types:
                if any(rdt.lower() in dt for dt in doc_types_lower):
                    matched = True
                    break
        
        # Check claim keyword rules
        required_keywords = rules.get("requires_any_claim_keyword", [])
        if required_keywords:
            for kw in required_keywords:
                if kw.lower() in claim_text:
                    matched = True
                    break
        
        # Check multi-jurisdiction rule
        if rules.get("requires_multi_jurisdiction") and len(jurisdictions) > 1:
            matched = True
        
        if matched:
            matching.append(addon_id)
    
    return matching
```

---

## Main Runner Function

```python
def run_checklist(
    case_id: str,
    template_override: str | None = None,
    skip_addons: bool = False,
) -> dict:
    """
    Run the full two-tier checklist for a case.
    
    Args:
        case_id: UUID of the case
        template_override: If set, use this Tier 1 template instead of universal
        skip_addons: If True, only run Tier 1 (useful for testing)
    
    Returns:
        Structured checklist result with per-task answers, confidence, provenance.
    """
    supabase = _get_supabase()
    graph = build_graph()
    
    # 1. Load registry
    registry = _load_registry()
    
    # 2. Load Tier 1 template
    tier1_id = template_override or "universal_commercial"
    tier1_template = _load_template(tier1_id)
    templates_used = [tier1_id]
    
    # 3. Detect and load Tier 2 add-ons
    addon_tasks = []
    if not skip_addons:
        addon_ids = _detect_addons(case_id, registry, supabase)
        templates_used.extend(addon_ids)
        for addon_id in addon_ids:
            addon_template = _load_template(addon_id)
            addon_tasks.extend(addon_template.get("tasks", []))
            print(f"  Add-on detected: {addon_template['template_name']}")
    
    # 4. Merge and sort all tasks
    all_tasks = tier1_template.get("tasks", []) + addon_tasks
    execution_order = _build_execution_order(all_tasks)
    
    print(f"\n{'='*60}")
    print(f"  Checklist: {tier1_template['template_name']}")
    print(f"  Case: {case_id}")
    print(f"  Templates: {', '.join(templates_used)}")
    print(f"  Total tasks: {len(execution_order)}")
    print(f"{'='*60}\n")
    
    # 5. Create checklist run record
    run_id = _create_run_record(supabase, case_id, templates_used, len(execution_order))
    
    # 6. Execute tasks
    completed_results: dict[str, dict] = {}
    session_base = f"checklist-{case_id}-{run_id}"
    
    for i, task in enumerate(execution_order, 1):
        task_id = task["id"]
        label = task["label"]
        agent_hint = task.get("agent", "general")
        required = task.get("required", True)
        
        print(f"  [{i}/{len(execution_order)}] {label}...")
        
        # Build query with dependency context
        full_query = _build_task_query(task, completed_results)
        
        # Build agent state
        config = {
            "configurable": {
                "thread_id": f"{session_base}-{task_id}",
                "case_id": case_id,
            }
        }
        
        state = {
            "messages": [HumanMessage(content=full_query)],
            "case_id": case_id,
            "tool_call_count": 0,
            "search_results": [],
            "kg_context": [],
            "extractions_context": [],
            "provenance_links": [],
            "reasoning_steps": [],
            "needs_review": False,
            "query_type": agent_hint,  # hint the router
            "agent_name": None,
            "answer": None,
            "confidence": None,
        }
        
        try:
            result = graph.invoke(state, config=config)
            
            completed_results[task_id] = {
                "task_id": task_id,
                "task_label": label,
                "template": task.get("_source_template", tier1_id),
                "layer": task.get("layer"),
                "tier": 2 if task_id.startswith("CD_") or task_id.startswith("SH_") else 1,
                "status": "completed",
                "answer": result.get("answer", ""),
                "confidence": result.get("confidence", 0),
                "needs_review": result.get("needs_review", False),
                "provenance_links": result.get("provenance_links", []),
                "reasoning_steps": result.get("reasoning_steps", []),
                "agent_used": result.get("agent_name", "unknown"),
            }
            
            status_icon = "⚠" if result.get("needs_review") else "✓"
            conf = result.get("confidence", 0)
            print(f"           {status_icon} confidence={conf:.2f}")
            
        except Exception as e:
            completed_results[task_id] = {
                "task_id": task_id,
                "task_label": label,
                "status": "failed",
                "error": str(e),
            }
            print(f"           ✗ FAILED: {e}")
            
            if required:
                print(f"           (required task — continuing but flagging)")
    
    # 7. Calculate overall metrics
    all_results = list(completed_results.values())
    completed_count = sum(1 for r in all_results if r["status"] == "completed")
    failed_count = sum(1 for r in all_results if r["status"] == "failed")
    flagged_count = sum(1 for r in all_results if r.get("needs_review", False))
    
    confidences = [
        r["confidence"] for r in all_results
        if r["status"] == "completed" and r.get("confidence") is not None
    ]
    overall_confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    
    # 8. Persist to Supabase
    _update_run_record(
        supabase, run_id,
        completed=completed_count,
        failed=failed_count,
        flagged=flagged_count,
        overall_confidence=overall_confidence,
        results=all_results,
        status="completed" if failed_count == 0 else "partial",
    )
    
    # 9. Print summary
    print(f"\n{'='*60}")
    print(f"  CHECKLIST COMPLETE")
    print(f"  Completed: {completed_count}/{len(all_results)}")
    print(f"  Failed: {failed_count}")
    print(f"  Flagged for review: {flagged_count}")
    print(f"  Overall confidence: {overall_confidence:.2f}")
    print(f"{'='*60}\n")
    
    return {
        "run_id": run_id,
        "case_id": case_id,
        "templates_used": templates_used,
        "total_tasks": len(all_results),
        "completed": completed_count,
        "failed": failed_count,
        "flagged_for_review": flagged_count,
        "overall_confidence": overall_confidence,
        "results": all_results,
    }
```

---

## CLI Interface

Add to `run.py`:

```python
parser.add_argument(
    "--checklist", action="store_true",
    help="Run the full case checklist (Tier 1 + auto-detected Tier 2 add-ons)."
)
parser.add_argument(
    "--checklist_tier1_only", action="store_true",
    help="Run only the Tier 1 universal checklist (skip add-on detection)."
)
```

Usage:
```bash
# Full checklist (universal + auto-detected add-ons)
python run.py --case_id "7d178a8c-..." --checklist

# Universal only (skip add-ons, good for testing)
python run.py --case_id "7d178a8c-..." --checklist --checklist_tier1_only

# Debug mode (show reasoning steps per task)
python run.py --case_id "7d178a8c-..." --checklist --debug
```

---

## Output Format

The checklist produces a structured JSON result. Example (abbreviated):

```json
{
  "run_id": "uuid-...",
  "case_id": "7d178a8c-...",
  "templates_used": ["universal_commercial", "addon_contract_dispute"],
  "total_tasks": 32,
  "completed": 30,
  "failed": 0,
  "flagged_for_review": 4,
  "overall_confidence": 0.78,
  "results": [
    {
      "task_id": "L1_3.2_parties",
      "task_label": "Parties and roles",
      "tier": 1,
      "layer": 1,
      "status": "completed",
      "answer": "This case involves two primary parties: Epic Games, Inc. (Plaintiff, corporation, North Carolina) and Apple Inc. (Defendant, corporation, California)...",
      "confidence": 0.92,
      "needs_review": false,
      "provenance_links": [
        {"section_id": "uuid-...", "file_name": "Complaint", "page_range": "1-3"}
      ],
      "agent_used": "general_agent"
    },
    {
      "task_id": "L2_4.6_breach_sequencing",
      "task_label": "Breach sequencing and attribution",
      "tier": 1,
      "layer": 2,
      "status": "completed",
      "answer": "Based on the timeline, Epic Games introduced a direct payment mechanism on August 13, 2020. Apple removed Fortnite the same day...",
      "confidence": 0.65,
      "needs_review": true,
      "provenance_links": [...],
      "agent_used": "cross_doc_agent"
    },
    {
      "task_id": "CD_termination_validity",
      "task_label": "Termination validity checklist",
      "tier": 2,
      "status": "completed",
      "answer": "Apple invoked Section 3.2 of the Developer Agreement (termination for cause). The triggering event was...",
      "confidence": 0.71,
      "needs_review": false,
      "provenance_links": [...],
      "agent_used": "cross_doc_agent"
    }
  ]
}
```

---

## Error Handling

- **Task failure:** Log the error, mark the task as "failed", continue to next task. Never crash the whole checklist because one task failed.
- **Dependency failure:** If a task's dependency failed, still run the task but inject a note: "Prior analysis task '{dep_id}' failed — proceed with available information." The agent may produce a lower-confidence result but it's better than skipping.
- **Agent timeout:** If a task takes > 120 seconds, kill it and mark as "timeout". Move on.
- **Rate limiting:** Add a 1-second sleep between tasks to avoid hitting Gemini/OpenAI rate limits. For the universal template (24 tasks), this adds ~24 seconds overhead.

---

## Estimated Effort

| Component | Effort |
|---|---|
| `checklist_runner.py` main function | 1 day |
| Dependency resolution (`_build_execution_order`) | 2 hours |
| Context injection (`_build_task_query`) | 1 hour |
| Add-on detection (`_detect_addons`) | 2 hours |
| Supabase persistence (`checklist_runs` table + helpers) | 1 hour |
| CLI integration in `run.py` | 30 min |
| Testing with Epic v. Apple case | Half day |

**Total: ~2 days**

---

## Testing

### Smoke Test

```bash
python run.py --case_id "7d178a8c-..." --checklist --checklist_tier1_only --debug
```

Expected: 24 tasks execute in order. Layer 1 tasks complete first, Layer 2 tasks get context from Layer 1. Each task shows the agent used, confidence, and source count.

### Add-On Detection Test

```bash
python run.py --case_id "7d178a8c-..." --checklist --debug
```

Expected: Detects `addon_contract_dispute` because the case has Contract documents and breach claims. Runs 24 + 8 = 32 tasks total.

### Dependency Test

Check that `L2_4.6_breach_sequencing` (depends on `L1_3.6_timeline` and `L1_3.9_claims`) receives context from both dependencies in its query. The agent should reference prior timeline/claims analysis rather than re-extracting from scratch.

### Persistence Test

```sql
SELECT id, templates_used, total_tasks, completed, flagged_for_review, overall_confidence, status
FROM checklist_runs
WHERE case_id = '7d178a8c-...'
ORDER BY started_at DESC
LIMIT 1;
```