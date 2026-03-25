"""
checklist_runner.py — Two-tier checklist orchestrator for the legal AI agent system.

Architecture:
  Tier 1 (universal_commercial): always runs — 12 Layer 1 foundation tasks +
                                  12 Layer 2 derived-insight tasks.
  Tier 2 (add-ons): auto-detected from case data, stack on top of Tier 1.
                    Each add-on task can reference Tier 1 results via depends_on_universal.

Execution order:
  1. Tier 1 Layer 1 tasks (no dependencies) — topologically sorted
  2. Tier 1 Layer 2 tasks (depend on Layer 1) — topologically sorted
  3. Tier 2 add-on tasks (depend on universal tasks) — topologically sorted

Usage:
    from checklist_runner import run_checklist
    result = run_checklist(case_id="7d178a8c-...", skip_addons=False, verbose=True)

    # CLI:
    python checklist_runner.py --case_id "7d178a8c-..." [--tier1_only] [--output results.json]
"""

import argparse
import heapq
import importlib.util as _ilu
import json
import os
import sys
import time
import uuid

_ARCH_DIR = os.path.dirname(os.path.abspath(__file__))
if _ARCH_DIR not in sys.path:
    sys.path.insert(0, _ARCH_DIR)

# Load graph via importlib (avoids digit-prefixed package issues)
_graph_spec = _ilu.spec_from_file_location(
    "graph_module", os.path.join(_ARCH_DIR, "graph.py")
)
_graph_mod = _ilu.module_from_spec(_graph_spec)
_graph_spec.loader.exec_module(_graph_mod)
build_graph = _graph_mod.build_graph

from langchain_core.messages import HumanMessage

TEMPLATES_DIR = os.path.join(_ARCH_DIR, "schemas", "checklist_templates")
REGISTRY_FILE = os.path.join(TEMPLATES_DIR, "template_registry.json")

_AGENT_TO_QUERY_TYPE = {
    "complaint": "complaint",
    "contract":  "contract",
    "cross_doc": "cross_doc",
    "general":   "general",
}


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _get_supabase():
    _project_root = os.path.abspath(os.path.join(_ARCH_DIR, "..", ".."))
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_project_root, ".env"))
    except ImportError:
        pass
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


def _create_run_record(sb, case_id: str, templates_used: list, total_tasks: int) -> str:
    """Insert initial checklist_runs record. Returns the run UUID."""
    try:
        resp = sb.table("checklist_runs").insert({
            "case_id":        case_id,
            "templates_used": templates_used,
            "total_tasks":    total_tasks,
            "status":         "running",
        }).execute()
        return resp.data[0]["id"] if resp.data else str(uuid.uuid4())
    except Exception:
        return str(uuid.uuid4())


def _update_run_record(
    sb,
    run_id: str,
    completed: int,
    failed: int,
    flagged: int,
    overall_confidence: float,
    results: list,
    status: str,
) -> None:
    """Update the checklist_runs record with final results."""
    try:
        from datetime import datetime, timezone
        sb.table("checklist_runs").update({
            "completed":           completed,
            "failed":              failed,
            "flagged_for_review":  flagged,
            "overall_confidence":  overall_confidence,
            "results":             results,
            "status":              status,
            "completed_at":        datetime.now(timezone.utc).isoformat(),
        }).eq("id", run_id).execute()
    except Exception:
        pass  # Persistence failure must not crash the runner


# ── Template loading ──────────────────────────────────────────────────────────

def _load_registry() -> dict:
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_template(template_id: str, registry: dict | None = None) -> dict:
    """Load a template JSON by template_id."""
    if registry is None:
        registry = _load_registry()

    all_entries = {
        **registry.get("tier_1", {}),
        **registry.get("tier_2", {}),
    }
    entry = all_entries.get(template_id)
    file_path = (
        os.path.join(TEMPLATES_DIR, entry["file"])
        if entry
        else os.path.join(TEMPLATES_DIR, f"{template_id}.json")
    )

    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"Template '{template_id}' not found. Available: {list(all_entries.keys())}"
        )
    with open(file_path, encoding="utf-8") as f:
        return json.load(f)


# ── Add-on detection ──────────────────────────────────────────────────────────

def _detect_addons(case_id: str, registry: dict, sb) -> list:
    """
    Check each Tier 2 add-on's detection_rules against the case data.
    Returns a list of matching template IDs.
    """
    # Fetch document types
    try:
        docs_resp = (
            sb.table("documents")
            .select("document_type")
            .eq("case_id", case_id)
            .execute()
        )
        doc_types_lower = [
            (d.get("document_type") or "").lower()
            for d in (docs_resp.data or [])
        ]
    except Exception:
        doc_types_lower = []

    # Fetch claim text from extractions
    try:
        doc_ids_resp = (
            sb.table("documents").select("id").eq("case_id", case_id).execute()
        )
        doc_ids = [d["id"] for d in (doc_ids_resp.data or [])]
        claim_text = ""
        if doc_ids:
            claims_resp = (
                sb.table("extractions")
                .select("entity_name, entity_value")
                .eq("extraction_type", "claim")
                .in_("document_id", doc_ids)
                .limit(100)
                .execute()
            )
            claim_text = " ".join(
                (c.get("entity_name") or "") + " " + (c.get("entity_value") or "")
                for c in (claims_resp.data or [])
            ).lower()
    except Exception:
        claim_text = ""

    matching = []
    for addon_id, addon_info in registry.get("tier_2", {}).items():
        if addon_info.get("status") != "built":
            continue

        rules = addon_info.get("detection_rules", {})
        matched = False

        for rdt in rules.get("requires_any_doc_type", []):
            if any(rdt.lower() in dt for dt in doc_types_lower):
                matched = True
                break

        if not matched:
            for kw in rules.get("requires_any_claim_keyword", []):
                if kw.lower() in claim_text:
                    matched = True
                    break

        if matched:
            matching.append(addon_id)

    return matching


# ── Dependency resolution ─────────────────────────────────────────────────────

def _build_execution_order(all_tasks: list) -> list:
    """
    Topological sort using Kahn's algorithm.

    Tie-breaking priority:
      1. Tier 1 before Tier 2
      2. Layer 1 before Layer 2
      3. Original JSON order within same priority level
    """
    task_map  = {t["id"]: t for t in all_tasks}
    task_idx  = {t["id"]: i for i, t in enumerate(all_tasks)}

    def _deps(task):
        return [
            d for d in
            task.get("depends_on", []) + task.get("depends_on_universal", [])
            if d in task_map
        ]

    # Build in-degree and adjacency list
    in_degree = {t["id"]: 0 for t in all_tasks}
    adj       = {t["id"]: [] for t in all_tasks}

    for task in all_tasks:
        for dep in _deps(task):
            adj[dep].append(task["id"])
            in_degree[task["id"]] += 1

    def _priority(task_id):
        t = task_map[task_id]
        return (t.get("tier", 1), t.get("layer", 1), task_idx[task_id])

    heap = []
    for task in all_tasks:
        if in_degree[task["id"]] == 0:
            heapq.heappush(heap, (_priority(task["id"]), task["id"]))

    order = []
    while heap:
        _, task_id = heapq.heappop(heap)
        order.append(task_map[task_id])
        for dep_id in adj[task_id]:
            in_degree[dep_id] -= 1
            if in_degree[dep_id] == 0:
                heapq.heappush(heap, (_priority(dep_id), dep_id))

    # Append any tasks caught in a cycle (shouldn't happen with valid templates)
    seen = {t["id"] for t in order}
    order.extend(t for t in all_tasks if t["id"] not in seen)
    return order


def _build_task_query(task: dict, completed_results: dict) -> str:
    """
    Build the full query for a task, injecting context from completed dependencies.
    Truncates each dependency answer to 2000 chars to keep context manageable.
    """
    deps = task.get("depends_on", []) + task.get("depends_on_universal", [])

    context_blocks = []
    for dep_id in deps:
        dep = completed_results.get(dep_id)
        if not dep:
            continue
        label = dep.get("task_label", dep_id)
        if dep.get("status") == "completed":
            answer = dep.get("answer", "")
            if len(answer) > 2000:
                answer = answer[:2000] + "\n[... truncated for context ...]"
            context_blocks.append(f"[{label} ({dep_id})]:\n{answer}")
        elif dep.get("status") == "failed":
            context_blocks.append(
                f"[{dep_id}]: Prior analysis task failed — "
                "proceed with available information. Results may be incomplete."
            )

    if context_blocks:
        ctx = "CONTEXT FROM PRIOR ANALYSIS:\n\n" + "\n\n".join(context_blocks)
        return f"{ctx}\n\nYOUR TASK:\n{task['query']}"
    return task["query"]


# ── Main runner ───────────────────────────────────────────────────────────────

def run_checklist(
    case_id: str,
    template_override: str | None = None,
    skip_addons: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Run the full two-tier checklist for a case.

    Args:
        case_id:           UUID of the case.
        template_override: Override the Tier 1 template ID (default: universal_commercial).
        skip_addons:       If True, skip Tier 2 add-on detection (useful for testing).
        verbose:           Print progress to stdout.

    Returns:
        Structured checklist result dict with per-task answers, confidence, provenance.
    """
    # Try Supabase — checklist still runs without it, just no persistence
    try:
        sb = _get_supabase()
        have_db = True
    except Exception:
        sb = None
        have_db = False
        if verbose:
            print("  Warning: Supabase unavailable — results will not be persisted.")

    registry = _load_registry()

    # ── Tier 1 ───────────────────────────────────────────────────────────────
    tier1_id = template_override or "universal_commercial"
    tier1_template = _load_template(tier1_id, registry)
    templates_used = [tier1_id]

    for task in tier1_template.get("tasks", []):
        task["_source_template"] = tier1_id
        task.setdefault("tier", 1)

    # ── Tier 2 add-ons ───────────────────────────────────────────────────────
    addon_tasks = []
    if skip_addons:
        if verbose:
            print("  Add-on detection skipped (tier1_only mode).")
    elif not have_db:
        if verbose:
            print("  Add-on detection skipped (no Supabase connection).")
    else:
        addon_ids = _detect_addons(case_id, registry, sb)
        for addon_id in addon_ids:
            addon_template = _load_template(addon_id, registry)
            for task in addon_template.get("tasks", []):
                task["_source_template"] = addon_id
                task.setdefault("tier", 2)
                task.setdefault("layer", 2)
            addon_tasks.extend(addon_template.get("tasks", []))
            templates_used.append(addon_id)
            if verbose:
                print(f"  Add-on detected: {addon_template['template_name']}")

    all_tasks = tier1_template.get("tasks", []) + addon_tasks
    execution_order = _build_execution_order(all_tasks)

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  Checklist: {tier1_template['template_name']}")
        print(f"  Case:      {case_id}")
        print(f"  Templates: {', '.join(templates_used)}")
        print(f"  Tasks:     {len(execution_order)}")
        print(f"{'=' * 60}\n")

    # Create Supabase run record
    run_id = (
        _create_run_record(sb, case_id, templates_used, len(execution_order))
        if have_db else str(uuid.uuid4())
    )

    graph = build_graph()
    session_base       = f"cl-{case_id[:8]}-{run_id[:8]}"
    completed_results  : dict[str, dict] = {}

    for i, task in enumerate(execution_order, 1):
        task_id  = task["id"]
        label    = task["label"]
        required = task.get("required", True)
        tier     = task.get("tier", 1)
        layer    = task.get("layer", 1)
        hint     = _AGENT_TO_QUERY_TYPE.get(task.get("agent", "general"), "general")

        if verbose:
            req_tag = "(req)" if required else "(opt)"
            print(f"  [{i:02d}/{len(execution_order):02d}] [T{tier}·L{layer}] {label} {req_tag}")

        full_query = _build_task_query(task, completed_results)

        config = {
            "configurable": {
                "thread_id": f"{session_base}-{task_id}",
                "case_id":   case_id,
            }
        }
        state = {
            "messages":            [HumanMessage(content=full_query)],
            "case_id":             case_id,
            "tool_call_count":     0,
            "search_results":      [],
            "kg_context":          [],
            "extractions_context": [],
            "provenance_links":    [],
            "reasoning_steps":     [],
            "needs_review":        False,
            "query_type":          hint,
            "agent_name":          None,
            "answer":              None,
            "confidence":          None,
        }

        try:
            result = graph.invoke(state, config=config)
            task_result = {
                "task_id":          task_id,
                "task_label":       label,
                "template":         task.get("_source_template", tier1_id),
                "tier":             tier,
                "layer":            layer,
                "status":           "completed",
                "answer":           result.get("answer", ""),
                "confidence":       result.get("confidence") or 0.0,
                "needs_review":     result.get("needs_review", False),
                "provenance_links": result.get("provenance_links", []),
                "reasoning_steps":  result.get("reasoning_steps", []),
                "agent_used":       result.get("agent_name", "unknown"),
                "required":         required,
            }

            if verbose:
                conf = task_result["confidence"]
                flag = " ⚠ NEEDS REVIEW" if task_result["needs_review"] else ""
                nsrc = len(task_result["provenance_links"])
                print(f"           ✓ conf={conf:.2f}{flag} | sources={nsrc}")

        except Exception as e:
            task_result = {
                "task_id":    task_id,
                "task_label": label,
                "template":   task.get("_source_template", tier1_id),
                "tier":       tier,
                "layer":      layer,
                "status":     "failed",
                "error":      str(e),
                "required":   required,
            }
            if verbose:
                print(f"           ✗ FAILED: {e}")
                if required:
                    print("           (required — continuing with flag)")

        completed_results[task_id] = task_result

        # Brief pause to avoid rate-limiting
        time.sleep(1)

    # ── Metrics ──────────────────────────────────────────────────────────────
    all_results     = list(completed_results.values())
    completed_count = sum(1 for r in all_results if r["status"] == "completed")
    failed_count    = sum(1 for r in all_results if r["status"] == "failed")
    flagged_count   = sum(1 for r in all_results if r.get("needs_review", False))

    confidences = [
        r["confidence"] for r in all_results
        if r["status"] == "completed" and r.get("confidence") is not None
    ]
    overall_confidence = (
        round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    )
    run_status = "completed" if failed_count == 0 else "partial"

    if have_db:
        _update_run_record(
            sb, run_id,
            completed=completed_count,
            failed=failed_count,
            flagged=flagged_count,
            overall_confidence=overall_confidence,
            results=all_results,
            status=run_status,
        )

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  CHECKLIST COMPLETE")
        print(f"  Completed: {completed_count}/{len(all_results)}")
        if failed_count:
            print(f"  Failed:    {failed_count}")
        if flagged_count:
            print(f"  Flagged:   {flagged_count}")
        print(f"  Confidence:{overall_confidence:.2f}")
        print(f"{'=' * 60}\n")

    return {
        "run_id":             run_id,
        "case_id":            case_id,
        "templates_used":     templates_used,
        "total_tasks":        len(all_results),
        "completed":          completed_count,
        "failed":             failed_count,
        "flagged_for_review": flagged_count,
        "overall_confidence": overall_confidence,
        "results":            all_results,
    }


# ── Pretty-print ──────────────────────────────────────────────────────────────

def _print_checklist_results(summary: dict) -> None:
    """Pretty-print a completed checklist summary."""
    print(f"\n{'=' * 60}")
    tpls = summary.get("templates_used", ["?"])
    print(f"  {tpls[0]}" + (f" + {len(tpls) - 1} add-on(s)" if len(tpls) > 1 else ""))
    print(f"  Case: {summary['case_id']}")
    parts = [f"{summary['completed']}/{summary['total_tasks']} completed"]
    if summary.get("failed"):
        parts.append(f"{summary['failed']} failed")
    if summary.get("flagged_for_review"):
        parts.append(f"{summary['flagged_for_review']} flagged")
    parts.append(f"confidence: {summary.get('overall_confidence', 0):.2f}")
    print(f"  {' | '.join(parts)}")
    print(f"{'=' * 60}\n")

    current_key = None
    for r in summary["results"]:
        section_key = (r.get("tier", 1), r.get("layer", 1))
        if section_key != current_key:
            current_key = section_key
            tier, layer = section_key
            print(f"\n── Tier {tier}  Layer {layer} ──")

        marker  = "✓" if r["status"] == "completed" else "✗"
        opt_tag = "" if r.get("required", True) else " [optional]"
        print(f"\n{marker} [{r['task_id']}] {r['task_label']}{opt_tag}")

        if r["status"] == "failed":
            print(f"  ERROR: {r.get('error', '?')}")
            continue

        conf = r.get("confidence", 0)
        flag = " ⚠ NEEDS REVIEW" if r.get("needs_review") else ""
        print(f"  Agent: {r.get('agent_used', '?')} | Confidence: {conf:.2f}{flag}")

        answer  = r.get("answer", "(no answer)")
        preview = answer[:300].replace("\n", " ").strip()
        if len(answer) > 300:
            preview += "..."
        print(f"  {preview}")

        links = r.get("provenance_links", [])
        if links:
            srcs = ", ".join(
                f"{lnk.get('file_name', '?')}"
                + (f" p.{lnk['page_range']}" if lnk.get("page_range") else "")
                for lnk in links[:3]
            )
            if len(links) > 3:
                srcs += f" +{len(links) - 3} more"
            print(f"  Sources: {srcs}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Legal AI Checklist Runner — two-tier case analysis."
    )
    parser.add_argument("--case_id", required=True, help="UUID of the case.")
    parser.add_argument(
        "--template", default=None,
        help="Override Tier 1 template ID (default: universal_commercial).",
    )
    parser.add_argument(
        "--tier1_only", action="store_true",
        help="Run only the Tier 1 universal template (skip add-on detection).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Optional path to write JSON results to.",
    )
    args = parser.parse_args()

    summary = run_checklist(
        case_id=args.case_id,
        template_override=args.template,
        skip_addons=args.tier1_only,
        verbose=True,
    )
    _print_checklist_results(summary)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"Results written to: {args.output}")


if __name__ == "__main__":
    main()
