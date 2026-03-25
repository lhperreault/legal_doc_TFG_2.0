# Phase 4B: Remaining Agents & Checklist System — Planning Document

## 1. What You Have (Built in Phase 4A)

### 1.1 Working Infrastructure
| Component | Status | Location |
|---|---|---|
| LangGraph state machine | Working | `04_AGENTIC_ARCHITECTURE/graph.py` |
| AgentState schema | Working | `state.py` — messages, context accumulators, HITL fields |
| Query classifier | Working | `nodes/classify.py` — routes to complaint/contract/general |
| Complaint agent | Working | `nodes/complaint_agent.py` — Gemini + tools, multi-turn |
| Response formatter + HITL | Working | `nodes/respond.py` — provenance, confidence, flagging |
| Tool: search_sections | Working | `tools/search.py` — wraps Phase 3 hybrid_search |
| Tool: get_claim_evidence | Working | `tools/kg_query.py` — KG claim→evidence paths |
| Tool: get_timeline | Working | `tools/kg_query.py` — chronological event ordering |
| Tool: query_extractions | Working | `tools/extractions.py` — structured entity lookup |
| Tool: query_kg | Working | `tools/kg_query.py` — general KG traversal |
| Persistence | Working | `persistence.py` — agent_responses + reviews tables |
| CLI runner | Working | `run.py` — single query + interactive multi-turn |

### 1.2 What's Missing
- Contract agent (specialized system prompt + contract-specific tool usage patterns)
- Cross-doc agent (multi-document reasoning, exhibit linking, conflict detection)
- Checklist agent (iterates a template, dispatches sub-queries to other agents)
- Case law agent (external precedent search — future, not in this doc)
- Graph routing to multiple agents (currently everything routes to complaint_agent)
- Agent-to-agent delegation (checklist agent calling complaint/contract agents)


---

## 2. What We're Building

### 2.1 Phase 4B Scope

| Agent | Purpose | Complexity |
|---|---|---|
| **Contract agent** | Contract terms, obligations, clauses, rights, conditions | Low — same pattern as complaint agent, different system prompt |
| **Cross-doc agent** | Multi-document reasoning, exhibit references, conflicts, comparisons | Medium — needs to combine results across documents |
| **Checklist agent** | Iterates a case-type template, dispatches tasks to other agents, tracks completion | Medium — orchestrates other agents, manages template state |
| **General agent** | Catch-all for timeline, overview, cross-cutting questions | Low — reuses existing tools with a general-purpose prompt |

### 2.2 What We're NOT Building Yet
- Case law agent (requires external data source integration)
- Frontend API endpoints (Phase 5)
- Review queue UI (Phase 5)
- Proactive case briefing automation (runs after Phase 5 frontend exists)


---

## 3. Contract Agent

### 3.1 Design

The contract agent follows the exact same LangGraph pattern as the complaint agent. It uses the same tools, same state schema, same response format. The only differences are:

1. **System prompt** — focused on contract analysis (clauses, obligations, rights, conditions, termination, indemnification)
2. **Default search filters** — when searching, it biases toward `document_type LIKE 'Contract%'` and `semantic_labels` in the contract ontology
3. **Tool usage patterns** — more likely to use `query_extractions(extraction_type="obligation")` and less likely to use `get_claim_evidence()`

### 3.2 System Prompt (`prompts/contract_system.md`)

```markdown
You are a legal document analysis agent specializing in contracts and commercial agreements.

You have access to a case database (case ID: {case_id}) through the following tools:

- **search_sections** — Find relevant contract sections by meaning or keyword. 
  TIP: Filter with document_types=["Contract"] and use semantic_labels like 
  "obligation.payment", "termination.for_cause", "indemnification.scope" for precise results.
- **query_extractions** — Look up extracted contract entities: obligations, conditions, amounts, parties, dates.
- **query_kg** — Find relationships between contract parties, obligations, and conditions.
- **get_timeline** — Build timeline of contractual events and deadlines.
- **get_claim_evidence** — Check if contract terms are referenced in complaint claims.

## Rules

1. Always cite the specific contract clause: article/section number, title, and page range.
2. When analyzing obligations, identify: who is obligated, what they must do, by when, and what happens if they don't.
3. When analyzing rights, identify: who holds the right, what they can do, and under what conditions.
4. For termination questions, trace the full chain: trigger event → notice requirement → cure period → termination effect.
5. If multiple contracts exist in the case, always specify WHICH contract you're referencing.
6. Cross-reference contract terms with complaint allegations when relevant — flag where a complaint claim maps to a specific clause.

[Confidence: X.XX | Sources: N sections cited]
```

### 3.3 Implementation

Create `nodes/contract_agent.py` — it's nearly identical to `complaint_agent.py`:

```python
# nodes/contract_agent.py
_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'prompts', 'contract_system.md'
)

# Same model, same tools, same pattern
_model_with_tools = _base_model.bind_tools(contract_tools)

def contract_agent_node(state: dict) -> dict:
    """Contract agent: same pattern as complaint agent."""
    case_id = state.get("case_id", "")
    system_message = SystemMessage(
        content=_SYSTEM_PROMPT_TEMPLATE.format(case_id=case_id)
    )
    response = _model_with_tools.invoke(
        [system_message] + state["messages"]
    )
    return {
        "messages": [response],
        "tool_call_count": state.get("tool_call_count", 0) + 1,
        "agent_name": "contract_agent",
    }
```

### 3.4 Tool Set

The contract agent uses the same 5 tools as the complaint agent. No new tools needed. The system prompt guides which tools it prefers.

```python
# tools/__init__.py
contract_tools = complaint_tools  # same tools, different prompt guides usage
```

### 3.5 Effort
Half a day. Copy complaint_agent.py, write the system prompt, wire into graph.py.


---

## 4. Cross-Document Agent

### 4.1 Why It's Different

The complaint and contract agents work primarily within a single document type. The cross-doc agent reasons across documents: "How does the complaint's breach allegation relate to the contract's termination clause?" or "Compare the obligations in Exhibit A with the allegations in the complaint."

This requires a different tool usage pattern: the agent searches multiple document types, traverses cross-document KG edges (exhibit_of, breached_by, same_as), and synthesizes information from different sources.

### 4.2 System Prompt (`prompts/cross_doc_system.md`)

```markdown
You are a legal document analysis agent specializing in cross-document reasoning.

You analyze relationships BETWEEN documents in a case — how a complaint references a contract, 
how exhibits support or contradict allegations, how obligations in one document relate to claims in another.

You have access to a case database (case ID: {case_id}) through the following tools:

- **search_sections** — Search across ALL document types. Do NOT filter by document_type 
  unless the user specifies one. Cast a wide net first.
- **query_kg** — THIS IS YOUR PRIMARY TOOL. Use edge_type filters to find cross-document relationships:
  - "exhibit_of" — links complaint exhibit references to exhibit document content
  - "breached_by" — links contract obligations to complaint breach claims  
  - "same_as" — links the same party/entity across different documents
  - "supported_by" — links claims to evidence
- **get_claim_evidence** — Trace evidence paths across documents.
- **query_extractions** — Compare entities across documents (e.g., same party in different roles).
- **get_timeline** — Build a unified timeline combining events from all documents.

## Rules

1. Always identify WHICH document each piece of information comes from.
2. When comparing across documents, present findings side-by-side:
   "The contract states X (Exhibit A, Section 3.2, p.45) but the complaint alleges Y (Complaint, ¶78, p.20)"
3. Use the knowledge graph to find relationships — don't just search twice and manually compare.
4. Flag contradictions and conflicts explicitly.
5. When tracing an exhibit reference, follow the full chain: 
   complaint mention → exhibit_of edge → exhibit document → specific clause.

[Confidence: X.XX | Sources: N sections from M documents cited]
```

### 4.3 When the Router Sends Queries Here

Update `classify.py` to route to cross_doc_agent when:
- The query mentions multiple documents ("compare the contract with the complaint")
- The query asks about relationships between documents ("how does Exhibit A relate to the breach claim")
- The query asks about conflicts or contradictions
- The query uses words like "across", "between", "compare", "cross-reference"

Add to the classification prompt:
```markdown
- **cross_doc** — Questions that span multiple documents: comparisons, cross-references, 
  exhibit tracing, conflict detection, or any question mentioning multiple document types.
```

### 4.4 Implementation

Same pattern as complaint_agent.py and contract_agent.py. Different system prompt. Same tools.

### 4.5 Effort
Half a day. Same pattern, different prompt, update router.


---

## 5. General Agent

### 5.1 Purpose

Catch-all for questions that don't fit complaint, contract, or cross-doc categories. Handles:
- "What is this case about?" (overview)
- "Who are all the parties?" (broad extraction query)
- "Build me a timeline" (graph analytics)
- "How many documents are in this case?" (metadata query)

### 5.2 Implementation

Same pattern. System prompt is generalist. Uses all tools without bias toward any document type.

### 5.3 Effort
Half a day.


---

## 6. Checklist Agent

### 6.1 How It's Different

The checklist agent is NOT a reasoning agent — it's an **orchestrator**. It reads a case-type template, iterates through tasks, and dispatches each task to the appropriate specialized agent. It doesn't call tools directly — it calls other agents.

### 6.2 Template Schema

```json
{
  "template_name": "Breach of Contract",
  "template_id": "breach_of_contract",
  "tasks": [
    {
      "id": "parties",
      "label": "Identify all parties and their roles",
      "query": "List all parties in this case with their roles (plaintiff, defendant, third-party, etc.) and the documents they appear in.",
      "agent": "general",
      "required": true
    },
    {
      "id": "claims",
      "label": "List all causes of action",
      "query": "What are all the causes of action or claims asserted in the complaint? For each, identify the legal basis and the parties involved.",
      "agent": "complaint",
      "required": true
    },
    {
      "id": "contract_terms",
      "label": "Extract key contract obligations",
      "query": "What are the key obligations in each contract? Identify who is obligated, what they must do, and any deadlines or conditions.",
      "agent": "contract",
      "required": true
    },
    {
      "id": "evidence_map",
      "label": "Map claims to supporting evidence",
      "query": "For each claim in the complaint, what evidence supports it? Flag any claims that have no evidence trail.",
      "agent": "cross_doc",
      "required": true
    },
    {
      "id": "timeline",
      "label": "Build case timeline",
      "query": "Build a chronological timeline of all events in this case, including contract dates, alleged breach dates, filing dates, and any other significant events.",
      "agent": "general",
      "required": true
    },
    {
      "id": "conflicts",
      "label": "Detect obligation conflicts",
      "query": "Are there any conflicting obligations across the contracts in this case? Identify any terms that contradict each other.",
      "agent": "cross_doc",
      "required": false
    },
    {
      "id": "damages",
      "label": "Summarize damages sought",
      "query": "What damages are sought in the complaint? Include specific amounts, types of damages (compensatory, punitive, statutory), and the basis for each.",
      "agent": "complaint",
      "required": false
    }
  ]
}
```

### 6.3 Architecture

The checklist agent doesn't fit the standard classify→agent→tools→respond pattern. It's a separate LangGraph graph (or a standalone Python function) that:

1. Loads the appropriate template based on case type (from `documents.document_type` — if the case has a complaint, use "Breach of Contract" template)
2. For each task in the template:
   a. Invokes the main agent graph with the task's query
   b. Captures the agent response (answer, confidence, provenance)
   c. Stores the result in a checklist state
3. Returns the completed checklist with per-task status

### 6.4 Implementation Approach

```python
# checklist_runner.py — NOT a LangGraph node, a standalone orchestrator

import json
from graph import build_graph
from langchain_core.messages import HumanMessage

TEMPLATES_DIR = "schemas/checklist_templates/"

def run_checklist(case_id: str, template_id: str = None) -> dict:
    """
    Run a case checklist by dispatching each task to the agent graph.
    
    If template_id is None, auto-detect from the case's document types.
    Returns a checklist result with per-task answers.
    """
    # Load template
    template = _load_template(template_id, case_id)
    
    # Build graph
    graph = build_graph()
    session_id = f"checklist-{case_id}-{template['template_id']}"
    
    results = []
    
    for task in template["tasks"]:
        config = {
            "configurable": {
                "thread_id": f"{session_id}-{task['id']}",
                "case_id": case_id,
            }
        }
        
        state = {
            "messages": [HumanMessage(content=task["query"])],
            "case_id": case_id,
            "tool_call_count": 0,
            "search_results": [],
            "kg_context": [],
            "extractions_context": [],
            "provenance_links": [],
            "reasoning_steps": [],
            "needs_review": False,
            "query_type": task.get("agent"),  # hint the router
            "agent_name": None,
            "answer": None,
            "confidence": None,
        }
        
        try:
            result = graph.invoke(state, config=config)
            results.append({
                "task_id": task["id"],
                "task_label": task["label"],
                "status": "completed",
                "answer": result.get("answer", ""),
                "confidence": result.get("confidence", 0),
                "needs_review": result.get("needs_review", False),
                "provenance_links": result.get("provenance_links", []),
                "agent_used": result.get("agent_name", "unknown"),
            })
        except Exception as e:
            results.append({
                "task_id": task["id"],
                "task_label": task["label"],
                "status": "error",
                "error": str(e),
            })
    
    # Calculate overall completion
    completed = sum(1 for r in results if r["status"] == "completed")
    flagged = sum(1 for r in results if r.get("needs_review", False))
    
    return {
        "case_id": case_id,
        "template": template["template_name"],
        "total_tasks": len(template["tasks"]),
        "completed": completed,
        "flagged_for_review": flagged,
        "results": results,
    }
```

### 6.5 Auto-Detection of Template

```python
def _detect_template(case_id: str) -> str:
    """Detect the right checklist template based on case document types."""
    sb = _get_supabase()
    docs = sb.table("documents").select("document_type").eq("case_id", case_id).execute()
    doc_types = [d["document_type"] for d in (docs.data or [])]
    
    has_complaint = any("Complaint" in (dt or "") for dt in doc_types)
    has_contract = any("Contract" in (dt or "") for dt in doc_types)
    
    if has_complaint and has_contract:
        return "breach_of_contract"
    elif has_complaint:
        return "general_litigation"
    elif has_contract:
        return "contract_review"
    else:
        return "general_case"
```

### 6.6 Effort
1.5 days. Template schema + runner + auto-detection + CLI wrapper.


---

## 7. Updated Graph Routing

### 7.1 Update `classify.py`

Add `cross_doc` to valid types:

```python
_VALID_TYPES = {"complaint", "contract", "cross_doc", "general", "clarification"}
```

Update the classification prompt to include cross_doc (see section 4.3).

### 7.2 Update `graph.py`

Add all agent nodes and their routing:

```python
# Add nodes
workflow.add_node("classify", classify)
workflow.add_node("complaint_agent", complaint_agent_node)
workflow.add_node("contract_agent", contract_agent_node)
workflow.add_node("cross_doc_agent", cross_doc_agent_node)
workflow.add_node("general_agent", general_agent_node)
workflow.add_node("tools", ToolNode(tools=all_tools))
workflow.add_node("respond", respond)

# Route from classify
workflow.add_conditional_edges("classify", route_by_type, {
    "complaint_agent": "complaint_agent",
    "contract_agent": "contract_agent",
    "cross_doc_agent": "cross_doc_agent",
    "general_agent": "general_agent",
})

# Each agent has the same tool loop
for agent_name in ["complaint_agent", "contract_agent", "cross_doc_agent", "general_agent"]:
    workflow.add_conditional_edges(agent_name, should_continue, {
        "tools": "tools",
        "respond": "respond",
    })

workflow.add_edge("tools", _route_back_to_agent)  # needs dynamic routing back
```

### 7.3 Dynamic Tool→Agent Routing

After tools execute, the graph needs to route back to whichever agent made the tool call. Currently it hardcodes `workflow.add_edge("tools", "complaint_agent")`. With multiple agents, use a conditional edge:

```python
def route_tools_back(state: dict) -> str:
    """After tools execute, route back to the agent that called them."""
    return state.get("agent_name", "complaint_agent")

workflow.add_conditional_edges("tools", route_tools_back, {
    "complaint_agent": "complaint_agent",
    "contract_agent": "contract_agent",
    "cross_doc_agent": "cross_doc_agent",
    "general_agent": "general_agent",
})
```


---

## 8. New Files to Create

| File | Purpose | Effort |
|---|---|---|
| `nodes/contract_agent.py` | Contract agent node | Half day |
| `nodes/cross_doc_agent.py` | Cross-document agent node | Half day |
| `nodes/general_agent.py` | General/overview agent node | Half day |
| `prompts/contract_system.md` | Contract agent system prompt | 1 hour |
| `prompts/cross_doc_system.md` | Cross-doc agent system prompt | 1 hour |
| `prompts/general_system.md` | General agent system prompt | 30 min |
| `checklist_runner.py` | Checklist orchestrator | 1 day |
| `schemas/checklist_templates/breach_of_contract.json` | Breach template | 1 hour |
| `schemas/checklist_templates/contract_review.json` | Contract review template | 1 hour |
| `schemas/checklist_templates/general_case.json` | General template | 30 min |

### Files to Modify

| File | Changes | Effort |
|---|---|---|
| `graph.py` | Add new agent nodes, update routing, dynamic tool→agent return | 2 hours |
| `nodes/classify.py` | Add cross_doc type, update prompt | 30 min |
| `prompts/classify_system.md` | Add cross_doc category description | 15 min |
| `tools/__init__.py` | Export tool sets per agent (if different) | 15 min |
| `run.py` | Add `--checklist` flag to CLI | 30 min |


---

## 9. Directory Structure (After Phase 4B)

```
backend/04_AGENTIC_ARCHITECTURE/
├── graph.py                          # Updated: 4 agents + routing
├── state.py                          # Unchanged
├── nodes/
│   ├── __init__.py                   # Updated: exports all agents
│   ├── classify.py                   # Updated: cross_doc routing
│   ├── complaint_agent.py            # Unchanged
│   ├── contract_agent.py             # NEW
│   ├── cross_doc_agent.py            # NEW
│   ├── general_agent.py              # NEW
│   └── respond.py                    # Unchanged
├── tools/
│   ├── __init__.py                   # Updated: tool sets per agent
│   ├── search.py                     # Unchanged
│   ├── kg_query.py                   # Unchanged
│   └── extractions.py                # Unchanged
├── prompts/
│   ├── classify_system.md            # Updated: cross_doc category
│   ├── complaint_system.md           # Unchanged
│   ├── contract_system.md            # NEW
│   ├── cross_doc_system.md           # NEW
│   └── general_system.md             # NEW
├── schemas/
│   ├── __init__.py
│   ├── response.py                   # Unchanged
│   └── checklist_templates/
│       ├── breach_of_contract.json   # NEW
│       ├── contract_review.json      # NEW
│       └── general_case.json         # NEW
├── checklist_runner.py               # NEW
├── persistence.py                    # Unchanged
├── run.py                            # Updated: --checklist flag
└── requirements.txt
```


---

## 10. Estimated Effort & Sequence

| Step | What | Effort | Depends On |
|---|---|---|---|
| 1 | Contract agent (node + prompt) | Half day | — |
| 2 | General agent (node + prompt) | Half day | — |
| 3 | Update graph.py routing + dynamic tool return | 2 hours | Steps 1-2 |
| 4 | Cross-doc agent (node + prompt) | Half day | Step 3 |
| 5 | Update classify.py for all categories | 1 hour | Steps 1-4 |
| 6 | Checklist templates (JSON files) | 2 hours | — |
| 7 | Checklist runner | 1 day | Steps 1-5 |
| 8 | CLI updates (--checklist flag) | 30 min | Step 7 |
| 9 | Testing + prompt tuning | 1 day | All above |

**Total: ~4-5 days of implementation.**


---

## 11. Testing Plan

### 11.1 Per-Agent Smoke Tests

**Contract agent:**
- "What are the payment obligations in the Developer Agreement?"
- "Can Apple terminate the agreement for convenience?"
- "What happens after termination?"

**Cross-doc agent:**
- "How does the complaint's breach allegation relate to the contract's Section 3.2?"
- "Compare the obligations in Exhibit A with what the complaint says Apple violated."
- "Which exhibit documents does the complaint reference?"

**General agent:**
- "What is this case about?"
- "Build me a timeline of events."
- "How many documents are in this case?"

### 11.2 Checklist Test

```bash
python run.py --case_id "7d178a8c-..." --checklist
# or
python checklist_runner.py --case_id "7d178a8c-..."
```

Should produce a completed checklist with per-task answers, confidence scores, and provenance. Tasks flagged with `needs_review: true` should have confidence < 0.7.

### 11.3 Router Accuracy Test

Run 20 diverse queries and check that classify.py routes to the right agent. Log the classification decisions and manually verify:
- Contract questions → contract_agent
- Complaint questions → complaint_agent  
- Cross-document questions → cross_doc_agent
- General questions → general_agent

### 11.4 Multi-Turn Test

Start an interactive session. Ask a complaint question, then a contract question, then a cross-doc question. Verify that the router switches agents correctly and that conversation context persists across turns.


---

## 12. What Comes After Phase 4B

With all agents + checklist working, the remaining pieces are:

1. **Case law agent** (Phase 4C) — requires external data source. Defer until you decide on the source (vLex API, CourtListener, or custom scraper).

2. **Proactive case briefing** — auto-run the checklist when a case finishes processing. This is just: `run_checklist(case_id)` triggered at the end of the Phase 2 pipeline. The results get stored in `agent_responses` and displayed on the case dashboard.

3. **Frontend API** (Phase 5) — FastAPI endpoints that wrap the agent graph for the frontend to call. `/api/query` for search, `/api/checklist` for checklists, `/api/review` for HITL queue.

4. **Frontend UI** (Phase 5) — case view, document view, search bar, checklist display, review queue, confidence heatmaps, comparative document view, interactive KG map.