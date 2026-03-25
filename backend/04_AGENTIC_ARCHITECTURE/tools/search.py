"""
tools/search.py — search_sections tool

Wraps hybrid_search() from 03_SEARCH/02_search.py.
case_id is injected at runtime via LangChain RunnableConfig.
"""

import os
import sys

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

# 02_search.py starts with a digit — Python can't import it directly.
# Use importlib to load it by file path.
import importlib.util as _ilu

_SEARCH_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '03_SEARCH')
)
_spec = _ilu.spec_from_file_location(
    "search_module", os.path.join(_SEARCH_DIR, "02_search.py")
)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
hybrid_search = _mod.hybrid_search


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def search_sections(
    query: str,
    document_types: list[str] | None = None,
    semantic_labels: list[str] | None = None,
    limit: int = 5,
    config: RunnableConfig = None,
) -> str:
    """Search for relevant document sections in the case using hybrid semantic + keyword search.

    Use this to find sections that discuss a topic, contain specific language,
    or match structural criteria (document type, section label). Returns section
    text with provenance (document name, page range, scores).

    Args:
        query: What to search for — natural language or exact legal terms.
        document_types: Optional filter, e.g. ["Pleading - Complaint", "Contract - License Agreement"].
        semantic_labels: Optional filter, e.g. ["causes_of_action", "factual_allegations", "obligations"].
        limit: Maximum number of sections to return (default 5, max 15).
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config. Cannot search."

    limit = min(limit, 15)

    try:
        results = hybrid_search(
            query=query,
            case_id=case_id,
            document_types=document_types,
            semantic_labels=semantic_labels,
            limit=limit,
        )
    except Exception as e:
        return f"ERROR: Search failed — {e}"

    if not results.get("results"):
        return f"No sections found for query: '{query}'"

    lines = [f"Search results for: '{query}' ({results['total_results']} found)\n"]
    for i, r in enumerate(results["results"], 1):
        lines.append(f"[{i}] {r.get('section_title') or '(untitled)'}")
        lines.append(f"    File: {r.get('file_name')} | Type: {r.get('document_type')}")
        lines.append(f"    Label: {r.get('semantic_label')} | Pages: {r.get('page_range')}")
        lines.append(f"    Score: {r['scores']['combined']:.3f}")
        text = (r.get("section_text") or "")[:400].replace("\n", " ")
        if text:
            lines.append(f"    Text: {text}{'...' if len(r.get('section_text','')) > 400 else ''}")
        lines.append("")

    return "\n".join(lines)
