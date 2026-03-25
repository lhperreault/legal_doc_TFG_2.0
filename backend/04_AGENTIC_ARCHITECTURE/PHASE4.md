# Phase 4: Agentic Workflows — Complete Planning Document

## 1. Scope of First Build

This document covers the MVP agent layer:
- **Query handler** (router) — classifies intent, routes to the right agent
- **Complaint agent** — answers questions about claims, evidence, parties, and cross-document relationships
- **HITL layers 1+2** — confidence flagging in agent responses + a reviews table for lawyer corrections
- **Multi-turn support** — agents can do follow-up searches and ask clarifying questions
- **LangGraph** as the orchestration framework

Future agents (contract, cross-doc, checklist, case law) follow the same pattern and reuse the shared tools and state schema.


---

## 2. What You Have (Input from Phases 1-3)

### 2.1 Shared Tools (already built)

| Tool | Location | What It Does |
|---|---|---|
| `hybrid_search()` | `backend/03_SEARCH/02_search.py` | Semantic + keyword + structural search over section embeddings. Returns ranked sections with provenance. |
| `build_timeline()` | `backend/02_MIDDLE/05_graph_analytics.py` | Chronological timeline from KG event nodes. |
| `find_claim_evidence_paths()` | `backend/02_MIDDLE/05_graph_analytics.py` | BFS from claim nodes to evidence/legal_authority nodes. Returns paths with hop count and confidence. |
| Supabase tables | `sections`, `extractions`, `kg_nodes`, `kg_edges`, `documents`, `section_embeddings` | Structured data for SQL queries. |

### 2.2 Data Available Per Case

After Phases 1-3 complete for a case, you have:
- **Sections** with text, AST hierarchy (parent_section_id, level), semantic labels, page ranges
- **Extractions** with typed entities (parties, claims, obligations, dates, amounts, evidence_refs, case_citations) linked to sections
- **KG nodes + edges** with intra-document relationships (alleged_by, supported_by, obligated_to, etc.) and cross-document relationships (same_as, exhibit_of, breached_by)
- **Vector embeddings** for semantic search over sections
- **Graph analytics** functions for timeline and claim-evidence analysis

### 2.3 What You Do NOT Have Yet
- A query classification/routing layer
- An LLM reasoning step that synthesizes search results into answers
- Conversation state management (multi-turn)
- A reviews/corrections table for HITL
- Agent response persistence (storing what the AI said for audit trail)


---

## 3. LangGraph Concepts (Quick Primer)

LangGraph models agent workflows as **state machines** (directed graphs). The key concepts:

### 3.1 State
A TypedDict or Pydantic model that holds everything the agent knows at any point in the conversation. Every node in the graph reads from and writes to this state. For our system, state includes: the user's query, conversation history, retrieved sections, KG context, the draft answer, confidence score, and provenance links.

### 3.2 Nodes
Functions that do one thing: classify the query, call a tool, reason with the LLM, format the response. Each node takes the current state, does work, and returns updated state fields.

### 3.3 Edges
Connections between nodes. Can be unconditional (always go from A to B) or conditional (go to B if the query is about claims, go to C if it's about parties). This is how routing works.

### 3.4 Graph
The compiled state machine. You define nodes, edges, and an entry point. LangGraph handles execution, state passing, and checkpointing (for multi-turn).

### 3.5 Checkpointer
Persists state between turns of a conversation. When the user sends a follow-up message, the checkpointer loads the previous state so the agent has context. We'll use `MemorySaver` for development (in-memory) and can switch to a Supabase-backed checkpointer for production.

### 3.6 Tool Calling
LangGraph integrates with LLM tool calling. You define tools (Python functions with docstrings), the LLM decides which tools to call, LangGraph executes the tool and feeds the result back to the LLM. This is how the agent decides to search, query the KG, or ask the user a clarifying question.


---

## 4. Architecture

### 4.1 The Graph (State Machine)

```
                    ┌─────────────┐
     User query ──→ │  classify   │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ↓            ↓            ↓
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │complaint │ │ contract │ │ general  │
        │  agent   │ │  agent   │ │  agent   │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │             │             │
             ↓             ↓             ↓
        ┌─────────────────────────────────────┐
        │           tool execution            │
        │  (search, KG, analytics, SQL)       │
        └──────────────────┬──────────────────┘
                           │
                           ↓
                    ┌──────────────┐
                    │   reason     │
                    │  (Gemini)    │
                    └──────┬───── ┘
                           │
                    ┌──────┴──────┐
                    │  needs more │──→ (loop back to tool execution)
                    │   info?     │
                    └──────┬──────┘
                           │ no
                           ↓
                    ┌──────────────┐
                    │   respond    │
                    │  (format +   │
                    │   HITL flag) │
                    └──────────────┘
```

### 4.2 State Schema

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # Conversation
    messages: Annotated[list, add_messages]  # full conversation history (HumanMessage, AIMessage, ToolMessage)
    
    # Routing
    case_id: str
    query_type: str | None          # "complaint", "contract", "general", "clarification"
    
    # Retrieved context (accumulated across tool calls)
    search_results: list[dict]      # from hybrid_search()
    kg_context: list[dict]          # from KG traversal / graph analytics
    extractions_context: list[dict] # from structured SQL queries
    
    # Agent output
    answer: str | None
    confidence: float | None
    provenance_links: list[dict]    # [{section_id, page_range, quote_snippet, file_name}]
    needs_review: bool
    reasoning_steps: list[str]
    
    # Control flow
    tool_call_count: int            # prevent infinite loops (max ~5 tool rounds)
    agent_name: str | None          # which specialized agent is active
```

### 4.3 Why This State Design

The `messages` field uses LangGraph's `add_messages` annotation — this automatically appends new messages instead of overwriting, preserving the full conversation history. This is what enables multi-turn: when the user sends a follow-up, the previous messages are still there.

The context fields (`search_results`, `kg_context`, `extractions_context`) accumulate across multiple tool calls within a single turn. If the agent searches, reads results, decides it needs more specific data, and searches again — all results are preserved.

The output fields (`answer`, `confidence`, `provenance_links`, etc.) match the HITL response schema from the architecture doc. Every agent populates these same fields.


---

## 5. Nodes (The Functions)

### 5.1 `classify` — Query Router

**Purpose:** Determine what type of question this is and route to the right agent.

**Implementation:** A lightweight Gemini call with a classification prompt. No tools needed — just read the query and return a category.

```python
def classify(state: AgentState) -> dict:
    """Classify the user's query and decide which agent handles it."""
    last_message = state["messages"][-1].content
    
    # Use Gemini to classify
    response = gemini_model.generate_content(
        f"""Classify this legal question into one category:
        - "complaint" — about claims, allegations, causes of action, evidence, parties in a complaint
        - "contract" — about contract terms, obligations, clauses, conditions
        - "general" — general case questions, timeline, cross-document
        - "clarification" — the question is unclear, need more info
        
        Question: {last_message}
        
        Return ONLY the category name, nothing else."""
    )
    
    return {"query_type": response.text.strip().lower()}
```

**Conditional edge after classify:**
```python
def route_by_type(state: AgentState) -> str:
    qt = state.get("query_type", "general")
    if qt == "complaint":
        return "complaint_agent"
    elif qt == "contract":
        return "contract_agent"
    else:
        return "general_agent"
```

### 5.2 `complaint_agent` — The Specialized Agent Node

**Purpose:** Handle complaint-related queries using the shared tools. This is where the LLM decides what tools to call.

**Implementation:** A Gemini call with tools bound. The LLM sees the query, the conversation history, and the tool descriptions. It decides whether to search, query the KG, check extractions, or respond directly.

```python
# Tools available to the complaint agent
complaint_tools = [
    search_sections_tool,       # wraps hybrid_search()
    get_claim_evidence_tool,    # wraps find_claim_evidence_paths()
    get_timeline_tool,          # wraps build_timeline()
    query_extractions_tool,     # structured SQL on extractions table
    query_kg_tool,              # KG node/edge traversal
]
```

**System prompt for complaint agent:**
```markdown
You are a legal document analysis agent specializing in complaints and litigation documents.

You have access to a case document database with the following tools:
- search_sections: Find relevant document sections by meaning or keyword
- get_claim_evidence: Trace paths from claims to supporting evidence
- get_timeline: Build chronological timeline of events
- query_extractions: Look up specific extracted entities (parties, claims, dates, amounts)
- query_kg: Traverse the knowledge graph for entity relationships

RULES:
1. Always ground your answers in specific document sections. Never speculate.
2. When citing a fact, include the section title, document name, and page range.
3. If you cannot find evidence for a claim, say so explicitly.
4. If the question is ambiguous, ask the user for clarification.
5. After using tools, synthesize the results into a clear answer.
6. Rate your confidence 0.0-1.0 based on how well the evidence supports your answer.
7. If confidence < 0.7, flag the answer for human review.

You are analyzing case: {case_id}
```

### 5.3 `tool_executor` — Run Tools

**Purpose:** LangGraph's built-in `ToolNode` executes whatever tools the LLM requested. It calls the Python functions, captures results, and adds them as `ToolMessage`s to the conversation.

```python
from langgraph.prebuilt import ToolNode

tool_node = ToolNode(tools=complaint_tools)
```

### 5.4 `should_continue` — Loop or Respond

**Purpose:** After tool execution, decide whether the agent needs another round of tool calls or is ready to respond.

```python
def should_continue(state: AgentState) -> str:
    last_message = state["messages"][-1]
    
    # If the LLM made tool calls, execute them
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        if state.get("tool_call_count", 0) >= 5:
            return "respond"  # safety limit
        return "tools"
    
    # No tool calls — the LLM is ready to respond
    return "respond"
```

### 5.5 `respond` — Format Output + HITL Flagging

**Purpose:** Take the LLM's final answer and structure it into the standard response schema with provenance and HITL flags.

```python
def respond(state: AgentState) -> dict:
    """Extract structured output from the conversation and apply HITL rules."""
    last_ai_message = state["messages"][-1].content
    
    # Parse provenance from the LLM's response (it should cite sections)
    provenance_links = _extract_provenance_from_response(last_ai_message, state["search_results"])
    
    # Calculate confidence based on evidence quality
    confidence = _calculate_confidence(provenance_links, state["search_results"])
    
    # HITL flagging
    needs_review = confidence < 0.7
    
    return {
        "answer": last_ai_message,
        "confidence": confidence,
        "provenance_links": provenance_links,
        "needs_review": needs_review,
        "reasoning_steps": _extract_reasoning_steps(state["messages"]),
    }
```


---

## 6. Tool Definitions

Each tool is a Python function with a docstring that the LLM reads to decide when/how to use it. LangGraph + Gemini handle the tool calling protocol.

### 6.1 `search_sections`

```python
from langchain_core.tools import tool

@tool
def search_sections(
    query: str,
    document_types: list[str] | None = None,
    semantic_labels: list[str] | None = None,
    limit: int = 5,
) -> str:
    """Search for relevant document sections in the case.
    
    Use this to find sections that discuss a topic, contain specific language,
    or match structural criteria. Returns section text with provenance.
    
    Args:
        query: What to search for (natural language or exact terms)
        document_types: Optional filter, e.g. ["Pleading - Complaint", "Contract - Agreement"]
        semantic_labels: Optional filter, e.g. ["causes_of_action.breach_of_contract", "factual_allegations"]
        limit: Max results (default 5)
    """
    # case_id comes from the graph state, injected at runtime
    results = hybrid_search(
        query=query,
        case_id=_get_case_id(),  # pulled from state
        document_types=document_types,
        semantic_labels=semantic_labels,
        limit=limit,
    )
    # Format for LLM consumption
    return _format_search_results(results)
```

### 6.2 `get_claim_evidence`

```python
@tool
def get_claim_evidence(claim_filter: str | None = None) -> str:
    """Find evidence paths for claims in the complaint.
    
    Traces paths from claim nodes to evidence and legal authority nodes
    in the knowledge graph. Shows which claims are supported and which are not.
    
    Args:
        claim_filter: Optional substring to filter claims (e.g. "breach", "antitrust")
    """
    nodes, edges = _fetch_kg_graph(case_id=_get_case_id())
    results = find_claim_evidence_paths(nodes, edges, claim_filter=claim_filter)
    return _format_claim_evidence(results)
```

### 6.3 `get_timeline`

```python
@tool
def get_timeline(party_filter: str | None = None) -> str:
    """Build a chronological timeline of events in the case.
    
    Args:
        party_filter: Optional party name to filter events (e.g. "Apple", "Epic")
    """
    nodes, edges = _fetch_kg_graph(case_id=_get_case_id())
    timeline = build_timeline(nodes, edges, party_filter=party_filter)
    return _format_timeline(timeline)
```

### 6.4 `query_extractions`

```python
@tool
def query_extractions(
    extraction_type: str,
    entity_name_contains: str | None = None,
    document_type: str | None = None,
) -> str:
    """Look up specific extracted entities from case documents.
    
    Use this for structured lookups like 'find all parties' or 'find all dates'.
    
    Args:
        extraction_type: One of: party, date, amount, obligation, claim, condition, evidence_ref, case_citation
        entity_name_contains: Optional substring filter on entity_name
        document_type: Optional filter on document type (e.g. "Pleading - Complaint")
    """
    # Direct SQL query on extractions table
    query = supabase.table("extractions").select("*").eq("extraction_type", extraction_type)
    if entity_name_contains:
        query = query.ilike("entity_name", f"%{entity_name_contains}%")
    # ... execute and format
```

### 6.5 `query_kg`

```python
@tool
def query_kg(
    node_type: str | None = None,
    edge_type: str | None = None,
    node_label_contains: str | None = None,
) -> str:
    """Traverse the knowledge graph for entity relationships.
    
    Use this to find how entities relate to each other across documents.
    
    Args:
        node_type: Filter nodes by type (party, claim, obligation, evidence, event, etc.)
        edge_type: Filter edges by type (alleged_by, supported_by, breached_by, exhibit_of, etc.)
        node_label_contains: Optional substring filter on node label
    """
    # Query kg_nodes and kg_edges with filters
    # Return formatted node-edge-node triples
```


---

## 7. The Compiled Graph

```python
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

# Build the graph
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("classify", classify)
workflow.add_node("complaint_agent", complaint_agent_node)
workflow.add_node("tools", ToolNode(tools=complaint_tools))
workflow.add_node("respond", respond)

# Add edges
workflow.add_edge(START, "classify")
workflow.add_conditional_edges("classify", route_by_type, {
    "complaint_agent": "complaint_agent",
    "contract_agent": "complaint_agent",  # TODO: add contract agent later
    "general_agent": "complaint_agent",   # TODO: add general agent later
})
workflow.add_conditional_edges("complaint_agent", should_continue, {
    "tools": "tools",
    "respond": "respond",
})
workflow.add_edge("tools", "complaint_agent")  # after tools, go back to agent for reasoning
workflow.add_edge("respond", END)

# Compile with checkpointer for multi-turn
memory = MemorySaver()
graph = workflow.compile(checkpointer=memory)
```

### 7.1 Running a Query

```python
# First turn
config = {"configurable": {"thread_id": "case-7d178a8c-session-1"}}
result = graph.invoke(
    {
        "messages": [HumanMessage(content="What are the main claims in the complaint?")],
        "case_id": "7d178a8c-eecb-42f6-b607-a3b847e4ec1e",
        "tool_call_count": 0,
        "search_results": [],
        "kg_context": [],
        "extractions_context": [],
        "provenance_links": [],
        "reasoning_steps": [],
        "needs_review": False,
    },
    config=config,
)

# Follow-up turn (state is preserved via thread_id)
result = graph.invoke(
    {"messages": [HumanMessage(content="Which of those claims has the weakest evidence?")]},
    config=config,
)
```

### 7.2 Multi-Turn Flow

The `thread_id` in the config ties turns together. The `MemorySaver` checkpointer stores the full state after each turn. On the second turn, the agent sees the full conversation history (previous query + answer + tool results) and can build on it without re-searching.

For production, replace `MemorySaver` with a Supabase-backed checkpointer that persists state across server restarts. LangGraph supports custom checkpointers — you'd implement `get_tuple()` and `put()` methods that read/write to a Supabase `agent_sessions` table.


---

## 8. HITL — Layers 1 and 2

### 8.1 Layer 1: Flagging (Built Into Agent Response)

Every agent response includes:

```python
{
    "answer": "The complaint alleges three causes of action...",
    "confidence": 0.82,
    "needs_review": False,       # True if confidence < 0.7
    "provenance_links": [...],
    "reasoning_steps": [...]
}
```

Confidence calculation considers:
- Number of provenance links (more sources = higher confidence)
- Semantic search scores of the retrieved sections (higher scores = better match)
- Whether the KG had relevant paths (claim → evidence path found = confidence boost)
- Whether any retrieved sections had `is_synthetic: true` (synthetic headings = slight penalty)

### 8.2 Layer 2: Storage (Reviews Table)

```sql
CREATE TABLE IF NOT EXISTS agent_responses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID NOT NULL REFERENCES cases(id),
    session_id          TEXT NOT NULL,           -- LangGraph thread_id
    query               TEXT NOT NULL,
    agent_name          TEXT NOT NULL,           -- "complaint_agent", "contract_agent"
    answer              TEXT NOT NULL,
    confidence          FLOAT NOT NULL,
    needs_review        BOOLEAN DEFAULT FALSE,
    provenance_links    JSONB,                   -- [{section_id, page_range, quote_snippet}]
    reasoning_steps     JSONB,                   -- ["step 1", "step 2"]
    tool_calls_made     JSONB,                   -- [{tool_name, args, result_summary}]
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reviews (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_response_id   UUID NOT NULL REFERENCES agent_responses(id),
    reviewer_id         TEXT,                    -- user ID of the lawyer who reviewed
    review_action       TEXT NOT NULL,           -- "approved", "corrected", "rejected"
    correction_text     TEXT,                    -- the lawyer's corrected answer (if corrected)
    correction_notes    TEXT,                    -- why the correction was made
    reviewed_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_agent_responses_case ON agent_responses (case_id);
CREATE INDEX idx_agent_responses_review ON agent_responses (needs_review, case_id);
CREATE INDEX idx_reviews_response ON reviews (agent_response_id);
```

### 8.3 How It Works End-to-End

1. Agent generates response → stored in `agent_responses` with `needs_review` flag
2. If `needs_review = true`, it appears in the review queue (frontend, Phase 5)
3. Lawyer reviews: approves, corrects, or rejects
4. Correction stored in `reviews` table
5. (Future) Corrections feed back into prompt engineering — if the agent consistently gets party names wrong, adjust the extraction prompt

### 8.4 Audit Trail

The combination of `agent_responses` + `reviews` creates a complete audit trail:
- What did the AI say? (`agent_responses.answer`)
- What evidence did it use? (`agent_responses.provenance_links`)
- How did it get there? (`agent_responses.reasoning_steps`, `tool_calls_made`)
- Did a human verify it? (`reviews.review_action`)
- Was it changed? (`reviews.correction_text`)

This is critical for legal work — a lawyer needs to show they verified AI outputs.


---

## 9. Gemini Integration

### 9.1 Model Choice

Use `gemini-2.5-flash` for agent reasoning (same model as entity extraction). It supports tool calling natively, has a large context window, and you already have the API key.

### 9.2 LangChain + Gemini Setup

```python
from langchain_google_genai import ChatGoogleGenerativeAI

model = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,            # low temperature for factual legal answers
    google_api_key=os.getenv("GEMINI_API_KEY"),
)

# Bind tools to the model
model_with_tools = model.bind_tools(complaint_tools)
```

### 9.3 The Agent Node

```python
def complaint_agent_node(state: AgentState) -> dict:
    """The complaint agent: calls Gemini with tools to answer the query."""
    system_message = SystemMessage(content=COMPLAINT_SYSTEM_PROMPT.format(
        case_id=state["case_id"],
    ))
    
    response = model_with_tools.invoke(
        [system_message] + state["messages"]
    )
    
    return {
        "messages": [response],
        "tool_call_count": state.get("tool_call_count", 0) + 1,
        "agent_name": "complaint_agent",
    }
```


---

## 10. Directory Structure

```
backend/04_AGENTIC_ARCHITECTURE
├── graph.py                    # The compiled LangGraph state machine
├── state.py                    # AgentState TypedDict
├── nodes/
│   ├── classify.py             # Query classification node
│   ├── complaint_agent.py      # Complaint agent node + system prompt
│   ├── respond.py              # Response formatting + HITL flagging
│   └── __init__.py
├── tools/
│   ├── search.py               # search_sections tool (wraps 03_SEARCH)
│   ├── kg_query.py             # query_kg + get_claim_evidence + get_timeline tools
│   ├── extractions.py          # query_extractions tool
│   └── __init__.py
├── prompts/
│   ├── complaint_system.md     # System prompt for complaint agent
│   └── classify_system.md      # System prompt for query classifier
├── schemas/
│   ├── response.py             # Pydantic model for agent response
│   └── __init__.py
├── persistence.py              # Save agent_responses to Supabase
├── run.py                      # CLI entry point for testing
└── requirements.txt            # langgraph, langchain-google-genai, etc.
```


---

## 11. Dependencies

### 11.1 New Python Libraries

| Library | Usage |
|---|---|
| `langgraph` | Agent orchestration state machine |
| `langchain-core` | Base classes: messages, tools, prompts |
| `langchain-google-genai` | Gemini integration for LangChain/LangGraph |

### 11.2 Install

```bash
pip install langgraph langchain-core langchain-google-genai
```

### 11.3 Environment Variables

No new env vars needed. Uses existing:
- `GEMINI_API_KEY` — for agent reasoning
- `OPENAI_API_KEY` — for query embedding (search tool)
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — for all data access


---

## 12. Estimated Effort & Sequence

| Step | What | Effort | Depends On |
|---|---|---|---|
| 1. SQL setup | `agent_responses` + `reviews` tables | 15 minutes | — |
| 2. State + tools | `state.py`, tool wrapper functions in `tools/` | 1 day | Phase 3 search working |
| 3. Classify node | `classify.py` with Gemini | Half day | Gemini key working |
| 4. Complaint agent node | `complaint_agent.py` with system prompt | 1 day | Tools working |
| 5. Respond node | `respond.py` with HITL flagging + persistence | Half day | Agent node working |
| 6. Graph compilation | `graph.py` wiring everything together | Half day | All nodes working |
| 7. CLI runner | `run.py` for testing | 2 hours | Graph compiled |
| 8. Testing + tuning | Prompt iteration, confidence calibration | 1 day | CLI runner working |

**Total: ~5 days of implementation.**


---

## 13. Testing Plan

### 13.1 Smoke Test Queries

Run these against the Epic v. Apple case and manually verify answers + provenance:

1. **"What are the main claims in the complaint?"** — Should list causes of action with section references. Tests: search + extraction query.

2. **"Which claims have supporting evidence?"** — Should use `get_claim_evidence` tool. Tests: KG traversal.

3. **"What happened in August 2020?"** — Should use timeline tool filtered by date. Tests: graph analytics.

4. **"Who are the parties?"** — Simple extraction query, no search needed. Tests: structured query tool.

5. **"Did Apple have the right to remove Fortnite?"** — Complex multi-tool query. Should search contract terms + complaint allegations + cross-reference via KG. Tests: multi-tool reasoning.

6. **Follow-up: "What evidence supports that conclusion?"** — Tests multi-turn: agent should reference the previous answer's context without re-searching everything.

### 13.2 HITL Verification

After running test queries:
```sql
-- Check that responses were stored
SELECT id, query, agent_name, confidence, needs_review 
FROM agent_responses 
WHERE case_id = '7d178a8c-eecb-42f6-b607-a3b847e4ec1e'
ORDER BY created_at DESC;

-- Check provenance links are populated
SELECT id, query, jsonb_array_length(provenance_links) as num_sources
FROM agent_responses 
WHERE case_id = '7d178a8c-eecb-42f6-b607-a3b847e4ec1e';
```

### 13.3 Edge Cases

- **Empty search results:** Agent should say "I couldn't find relevant sections" not hallucinate.
- **Ambiguous query:** Agent should ask for clarification ("Do you mean the Developer Agreement or the Terms of Service?").
- **Very long context:** If search returns many results, agent should prioritize high-scoring ones, not dump everything into the prompt.
- **Cross-document query on single-doc case:** Should gracefully handle cases with only one document.