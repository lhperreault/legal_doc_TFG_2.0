"""
run.py — CLI entry point for testing the agent.

Usage:
    # Single query
    python backend/04_AGENTIC_ARCHITECTURE/run.py \\
        --case_id "7d178a8c-eecb-42f6-b607-a3b847e4ec1e" \\
        --query "What are the main claims in the complaint?"

    # Interactive multi-turn session
    python backend/04_AGENTIC_ARCHITECTURE/run.py \\
        --case_id "7d178a8c-eecb-42f6-b607-a3b847e4ec1e" \\
        --interactive

    # Show raw state output (for debugging)
    python backend/04_AGENTIC_ARCHITECTURE/run.py \\
        --case_id "7d178a8c-eecb-42f6-b607-a3b847e4ec1e" \\
        --query "Who are the parties?" \\
        --debug
"""

import argparse
import json
import os
import sys
import uuid

# Ensure the project root is on the path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, '.env'))

from langchain_core.messages import HumanMessage

# Import graph (triggers model + tool initialization)
import importlib.util as _ilu
_graph_spec = _ilu.spec_from_file_location(
    "graph_module",
    os.path.join(os.path.dirname(__file__), "graph.py"),
)
_graph_mod = _ilu.module_from_spec(_graph_spec)
_graph_spec.loader.exec_module(_graph_mod)
build_graph = _graph_mod.build_graph


def _make_initial_state(case_id: str, query: str) -> dict:
    return {
        "messages":            [HumanMessage(content=query)],
        "case_id":             case_id,
        "tool_call_count":     0,
        "search_results":      [],
        "kg_context":          [],
        "extractions_context": [],
        "provenance_links":    [],
        "reasoning_steps":     [],
        "needs_review":        False,
        "query_type":          None,
        "agent_name":          None,
        "answer":              None,
        "confidence":          None,
    }


def _make_followup_state(query: str) -> dict:
    """For follow-up turns: only new message + reset accumulators."""
    return {
        "messages":            [HumanMessage(content=query)],
        "tool_call_count":     0,
        "search_results":      [],
        "kg_context":          [],
        "extractions_context": [],
    }


def _print_result(result: dict, debug: bool = False) -> None:
    print("\n" + "=" * 60)

    query_type = result.get("query_type", "?")
    agent_name = result.get("agent_name", "?")
    confidence = result.get("confidence") or 0
    needs_review = result.get("needs_review", False)

    print(f"  Agent: {agent_name} | Type: {query_type}")
    print(f"  Confidence: {confidence:.2f} {'⚠ NEEDS REVIEW' if needs_review else '✓'}")
    print("=" * 60)

    answer = result.get("answer", "(no answer)")
    print(f"\n{answer}\n")

    # Provenance links
    links = result.get("provenance_links", [])
    if links:
        print(f"─── Sources ({len(links)}) ───")
        for lnk in links:
            fname  = lnk.get("file_name", "?")
            pages  = lnk.get("page_range", "")
            page_s = f" p.{pages}" if pages else ""
            dtype  = lnk.get("document_type", "")
            print(f"  • {fname}{page_s} [{dtype}]")
            if lnk.get("quote_snippet"):
                snip = lnk["quote_snippet"][:120].replace("\n", " ")
                print(f"    \"{snip}...\"")
        print()

    if debug:
        print("─── Reasoning Steps ───")
        for step in result.get("reasoning_steps", []):
            print(f"  {step}")
        print()


def run_single(case_id: str, query: str, debug: bool = False) -> dict:
    """Run a single query and return the result state."""
    graph      = build_graph()
    session_id = str(uuid.uuid4())
    config     = {
        "configurable": {
            "thread_id": f"case-{case_id}-{session_id}",
            "case_id":   case_id,
        }
    }

    print(f"\nQuery: {query}")
    print("Processing...")

    state  = _make_initial_state(case_id, query)
    result = graph.invoke(state, config=config)

    _print_result(result, debug=debug)
    return result


def run_interactive(case_id: str, debug: bool = False) -> None:
    """Multi-turn interactive session. Type 'exit' or Ctrl+C to quit."""
    graph      = build_graph()
    session_id = str(uuid.uuid4())
    thread_id  = f"case-{case_id}-{session_id}"
    config     = {
        "configurable": {
            "thread_id": thread_id,
            "case_id":   case_id,
        }
    }

    print(f"\nLegal AI Agent — Case {case_id}")
    print(f"Session: {session_id}")
    print("Type 'exit' to quit, 'debug' to toggle debug mode.\n")

    first_turn = True

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSession ended.")
            break

        if not query:
            continue
        if query.lower() == "exit":
            print("Session ended.")
            break
        if query.lower() == "debug":
            debug = not debug
            print(f"Debug mode: {'ON' if debug else 'OFF'}")
            continue

        print("Processing...")

        if first_turn:
            state = _make_initial_state(case_id, query)
            first_turn = False
        else:
            state = _make_followup_state(query)

        try:
            result = graph.invoke(state, config=config)
            _print_result(result, debug=debug)
        except Exception as e:
            print(f"\nERROR: {e}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Legal AI Agent — CLI runner for testing."
    )
    parser.add_argument(
        "--case_id", required=True,
        help="UUID of the case to query."
    )
    parser.add_argument(
        "--query", default=None,
        help="Single query to run. If omitted, starts interactive session."
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Start a multi-turn interactive session."
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show reasoning steps and tool calls in output."
    )
    args = parser.parse_args()

    if args.query and not args.interactive:
        run_single(args.case_id, args.query, debug=args.debug)
    else:
        run_interactive(args.case_id, debug=args.debug)


if __name__ == "__main__":
    main()
