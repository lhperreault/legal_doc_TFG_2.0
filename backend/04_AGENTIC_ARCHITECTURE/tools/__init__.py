# tools/__init__.py

from .kg_query import query_kg, get_claim_evidence, get_timeline
from .search import search_sections
from .extractions import query_extractions
from .evidence_tools import match_evidence, detect_evidence_gaps, link_evidence_batch

complaint_tools = [
    search_sections,
    query_extractions,
    query_kg,
    get_claim_evidence,
    get_timeline,
    # New evidence tools
    match_evidence,
    detect_evidence_gaps,
    link_evidence_batch,
]
__all__ = [
    "search_sections",
    "get_claim_evidence",
    "get_timeline",
    "query_extractions",
    "query_kg",
    "complaint_tools",
]
