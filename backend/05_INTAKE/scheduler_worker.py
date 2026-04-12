"""
Scheduler worker: dispatches confirmed intake items to the pipeline by priority.

Run as a periodic tick (every 30s) or as a one-shot for testing.

Usage:
    python backend/05_INTAKE/scheduler_worker.py              # one tick
    python backend/05_INTAKE/scheduler_worker.py --loop       # continuous (30s interval)
    python backend/05_INTAKE/scheduler_worker.py --drain-overnight  # drain overnight items now
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
PHASE1_MAIN = os.path.join(BACKEND_DIR, "01_INITIAL", "main.py")
LOGS_DIR    = os.path.join(BACKEND_DIR, "data_storage", "logs")


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


# ── Priority defaults ────────────────────────────────────────────────────

PRIORITY_MODE_MAP = {
    "immediate": "fast",
    "soon":      "balanced",
    "overnight": "accuracy",
    "manual":    "balanced",
}


# ── Dispatch a single intake item ────────────────────────────────────────

async def dispatch_intake_item(
    intake_id: uuid.UUID,
    *,
    processing_mode: str | None = None,
    mode: str = "interactive",
) -> dict:
    """Run the pipeline for a single intake item. Updates status throughout."""
    sb = _get_sb()

    # Fetch the intake row
    resp = sb.table("intake_queue").select("*").eq("id", str(intake_id)).execute()
    if not resp.data:
        return {"error": "intake item not found"}
    item = resp.data[0]

    # Determine processing mode
    pm = processing_mode or item.get("processing_mode") or PRIORITY_MODE_MAP.get(item["process_priority"], "balanced")

    # Mark as processing
    sb.table("intake_queue").update({
        "status": "processing",
        "processing_mode": pm,
    }).eq("id", str(intake_id)).execute()

    # Run Phase 1 pipeline
    file_path = item.get("file_path") or ""
    file_name = item.get("file_name") or os.path.basename(file_path)
    case_id   = item.get("target_case_id") or ""
    corpus_id = item.get("target_corpus_id") or ""
    firm_id   = item.get("firm_id") or ""

    args = [
        sys.executable, PHASE1_MAIN, file_name,
        "--case-id", case_id,
        "--mode", mode,
        "--processing-mode", pm,
    ]
    if corpus_id:
        args += ["--corpus-id", corpus_id]
    if firm_id:
        args += ["--firm-id", firm_id]

    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"intake_{str(intake_id)[:8]}.log")

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=600,  # 10 min timeout
        )

        if result.returncode == 0:
            sb.table("intake_queue").update({
                "status": "completed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", str(intake_id)).execute()

            # Emit notification
            sys.path.insert(0, os.path.dirname(__file__))
            from notifications import notify_intake_completed
            case_name = _get_case_name(sb, case_id) or "Unknown"
            notify_intake_completed(firm_id, str(intake_id), file_name, case_name)

            return {"status": "completed", "intake_id": str(intake_id)}
        else:
            error_msg = (result.stderr or result.stdout or "Unknown error")[-500:]
            sb.table("intake_queue").update({
                "status": "failed",
                "error_message": error_msg,
                "retry_count": item.get("retry_count", 0) + 1,
            }).eq("id", str(intake_id)).execute()

            from notifications import notify_intake_failed
            notify_intake_failed(firm_id, str(intake_id), file_name, error_msg[:200])

            return {"status": "failed", "error": error_msg}

    except subprocess.TimeoutExpired:
        sb.table("intake_queue").update({
            "status": "failed",
            "error_message": "Pipeline timed out after 10 minutes",
        }).eq("id", str(intake_id)).execute()
        return {"status": "failed", "error": "timeout"}

    except Exception as e:
        sb.table("intake_queue").update({
            "status": "failed",
            "error_message": str(e)[:500],
        }).eq("id", str(intake_id)).execute()
        return {"status": "failed", "error": str(e)}


def _get_case_name(sb, case_id: str) -> str | None:
    if not case_id:
        return None
    try:
        resp = sb.table("cases").select("case_name").eq("id", case_id).execute()
        return resp.data[0]["case_name"] if resp.data else None
    except Exception:
        return None


# ── Scheduler tick ───────────────────────────────────────────────────────

async def run_scheduler_tick(
    *,
    max_concurrent: int = 3,
    batch_size: int = 10,
    priorities: tuple[str, ...] = ("immediate", "soon"),
) -> dict:
    """Pick confirmed/scheduled items by priority, dispatch to pipeline.

    Returns: {dispatched, skipped, errors, remaining}
    """
    sb = _get_sb()
    stats = {"dispatched": 0, "skipped": 0, "errors": 0, "remaining": 0}

    # Fetch items ready to process
    q = (
        sb.table("intake_queue")
        .select("id, process_priority, processing_mode, file_name")
        .in_("status", ["confirmed", "scheduled"])
        .in_("process_priority", list(priorities))
        .order("created_at")
        .limit(batch_size)
    )
    resp = q.execute()
    items = resp.data or []

    if not items:
        return stats

    sem = asyncio.Semaphore(max_concurrent)

    async def _dispatch_one(item):
        async with sem:
            result = await dispatch_intake_item(uuid.UUID(item["id"]))
            if result.get("status") == "completed":
                stats["dispatched"] += 1
            elif result.get("status") == "failed":
                stats["errors"] += 1
            else:
                stats["skipped"] += 1

    await asyncio.gather(*[_dispatch_one(item) for item in items])

    # Count remaining
    remaining_resp = (
        sb.table("intake_queue")
        .select("id", count="exact")
        .in_("status", ["confirmed", "scheduled"])
        .execute()
    )
    stats["remaining"] = remaining_resp.count or 0

    return stats


# ── Overnight batch runner ───────────────────────────────────────────────

async def run_overnight_batch(
    *,
    max_parallel: int = 5,
    processing_mode: str = "accuracy",
    dry_run: bool = False,
) -> dict:
    """Drain overnight-priority items with controlled parallelism."""
    sb = _get_sb()

    resp = (
        sb.table("intake_queue")
        .select("id, file_name, firm_id")
        .in_("status", ["confirmed", "scheduled"])
        .eq("process_priority", "overnight")
        .order("created_at")
        .execute()
    )
    items = resp.data or []
    stats = {"total_items": len(items), "completed": 0, "failed": 0, "skipped": 0}

    if dry_run:
        print(f"[overnight] Dry run: would process {len(items)} items")
        for item in items:
            print(f"  - {item['file_name']}")
        return stats

    print(f"[overnight] Processing {len(items)} items (mode={processing_mode}, parallel={max_parallel})")
    sem = asyncio.Semaphore(max_parallel)
    t0 = time.time()

    async def _process(item):
        async with sem:
            result = await dispatch_intake_item(
                uuid.UUID(item["id"]),
                processing_mode=processing_mode,
                mode="bulk",
            )
            if result.get("status") == "completed":
                stats["completed"] += 1
            else:
                stats["failed"] += 1

    await asyncio.gather(*[_process(item) for item in items])

    stats["duration_seconds"] = round(time.time() - t0, 1)

    # Generate morning summary per firm
    firm_ids = set(item["firm_id"] for item in items if item.get("firm_id"))
    for fid in firm_ids:
        sys.path.insert(0, os.path.dirname(__file__))
        from notifications import notify_morning_summary
        notify_morning_summary(fid, {
            "total_processed": stats["completed"],
            "failed": stats["failed"],
            "duration_seconds": stats["duration_seconds"],
            "message": f"Overnight batch complete: {stats['completed']} documents processed, {stats['failed']} failed",
        })

    print(f"[overnight] Done: {stats}")
    return stats


# ── CLI ──────────────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="Intake scheduler worker")
    parser.add_argument("--loop", action="store_true", help="Run continuously (30s interval)")
    parser.add_argument("--drain-overnight", action="store_true", help="Process all overnight items now")
    parser.add_argument("--interval", type=int, default=30, help="Tick interval in seconds (with --loop)")
    args = parser.parse_args()

    if args.drain_overnight:
        stats = await run_overnight_batch()
        print(f"Overnight batch: {stats}")
    elif args.loop:
        print(f"[scheduler] Starting continuous loop (interval={args.interval}s)")
        while True:
            stats = await run_scheduler_tick()
            if stats["dispatched"] or stats["errors"]:
                print(f"[scheduler] tick: {stats}")
            await asyncio.sleep(args.interval)
    else:
        stats = await run_scheduler_tick()
        print(f"Scheduler tick: {stats}")


if __name__ == "__main__":
    asyncio.run(_main())
