# Phase 3-5 Architecture & Product Vision

## Document Purpose
Captures all architectural decisions, agent design, and product ideas from the Phase 3 planning session. Reference this when building 03_SEARCH, 04_AGENTS, and frontend.

---

## 1. RAG Architecture — Hybrid → Agentic

### What We're Building
**Phase 3** = Hybrid RAG (retrieval only, no generation)
- Semantic search (pgvector cosine similarity)
- Keyword search (pg_trgm trigram matching)  
- Structural filters (AST metadata: semantic_label, document_type, level)
- Always scoped to case_id
- Returns ranked sections with provenance, not answers

**Phase 4** = Agentic RAG (retrieval + reasoning + generation)
- Agents use Phase 3 search as a tool
- KG traversal for relationship reasoning
- Graph analytics for timeline/conflict/evidence analysis
- LLM synthesizes answers with provenance chains
- Multiple specialized agents, one router

### Locked-In Decisions (Phase 3)
| Decision | Choice | Rationale |
|---|---|---|
| Embedding model | OpenAI text-embedding-3-small | 1536 dims, 8191 token context, $0.02/1M tokens, already have key |
| Vector store | Supabase pgvector | Single DB for everything, no sync issues, fine for per-case scale |
| What gets embedded | Sections only (enriched metadata) | Extractions/KG are metadata filters + structured queries, not embeddings |
| Search type | Hybrid: semantic + keyword + structural | Catches meaning AND exact terms AND structural context |
| Search scope | Always case_id filtered | Data isolation, governance, performance |
| Consumers | Agents (Phase 4) AND user-facing search endpoint | Same underlying function, different wrappers |

---

## 2. Agent Architecture

### Hierarchy: Surfaces → Router → Agents → Tools

```
User Surfaces (frontend)
├── Document view — single doc display, inline summaries
├── Case view — multi-doc overview, dashboard
├── Search bar — free-text query
└── Case checklist — auto-fill tasks per case type

        ↓ all call ↓

Query Handler / Router (one per case session)
├── Classifies intent
├── Picks agent(s)
└── Aggregates results

        ↓ dispatches to ↓

Specialized Agents (reusable across surfaces)
├── Contract agent — clauses, obligations, rights
├── Complaint agent — claims, evidence, causes of action
├── Cross-doc agent — multi-doc reasoning, exhibit linking
├── Case law agent — external precedent search
└── Checklist agent — iterates template, dispatches sub-queries

        ↓ all use ↓

Shared Tool Layer
├── hybrid_search() — Phase 3 vector + keyword + structural
├── kg_traverse() — walk KG nodes/edges, shortest paths
├── graph_analytics() — timeline, claim-evidence, conflicts
├── structured_query() — direct SQL on extractions table
├── llm_reason() — GPT / Claude / Gemini for synthesis
└── external_search() — case law databases (future)

        ↓ all read from ↓

Supabase (single database)
├── sections + section_embeddings (search)
├── extractions (structured facts)
├── kg_nodes + kg_edges (relationships)
└── documents + cases (metadata)
```

### Key Design Principles

**Agents are reusable.** The contract agent is the same code whether called from document view, case view, or checklist. It receives a task, uses tools, returns structured output.

**One router, not many.** The query handler is the single entry point. Surfaces don't talk to agents directly — they tell the router what they need.

**Structured response schema.** Every agent returns the same Pydantic shape:
- answer_text
- confidence (float 0-1)
- provenance_links (list of {section_id, page_range, quote_snippet})
- needs_review (boolean)
- reasoning_steps (list of strings, for transparency)

**Surfaces render, agents reason.** The frontend decides how to display the structured response (inline, card, checklist item). Agents decide what the answer is. The surface validates provenance links exist before rendering.

### File Organization
```
backend/04_AGENTS/
├── query_handler.py
├── agents/
│   ├── contract_agent.py
│   ├── complaint_agent.py
│   ├── cross_doc_agent.py
│   ├── case_law_agent.py
│   └── checklist_agent.py
├── prompts/
│   ├── contract_system.md
│   ├── complaint_system.md
│   └── ...
├── schemas/
│   ├── agent_response.py
│   └── checklist_templates/
│       ├── breach_of_contract.json
│       ├── personal_injury.json
│       └── m_and_a_due_diligence.json
└── tools/
    ├── search.py
    ├── kg_query.py
    ├── graph_analytics.py
    └── structured_query.py
```

### Case Checklist Design

Checklists are **workflow templates**, not agents. A template defines a list of tasks per case type:

```json
{
  "template_name": "Breach of Contract",
  "tasks": [
    {"id": "parties", "question": "Identify all parties and their roles", "agent": "complaint_agent"},
    {"id": "claims", "question": "List all causes of action", "agent": "complaint_agent"},
    {"id": "evidence_map", "question": "Map each claim to supporting evidence", "agent": "cross_doc_agent"},
    {"id": "unsupported", "question": "Flag claims with no evidence trail", "agent": "cross_doc_agent"},
    {"id": "obligations", "question": "Extract all contract obligations", "agent": "contract_agent"},
    {"id": "timeline", "question": "Build chronological timeline of events", "agent": "cross_doc_agent"},
    {"id": "conflicts", "question": "Detect conflicting obligations across contracts", "agent": "cross_doc_agent"},
    {"id": "damages", "question": "Summarize damages sought", "agent": "complaint_agent"}
  ]
}
```

The checklist agent iterates through tasks, dispatches each to the right specialized agent, collects results, and presents the checklist with completion status and confidence.

---

## 3. Product Ideas — Differentiation

### Idea 1: Living Case Map (Interactive KG Visualization)
**What:** Force-directed graph in the case view. Parties as nodes, documents as colored regions, claims as edges. Unsupported claims glow red. Click a node → side panel shows details with provenance.
**Why it's different:** No competitor shows case structure visually. Lawyers navigate document lists — we show the case as a map.
**Data source:** kg_nodes, kg_edges, graph_analytics (unsupported claims).
**Phase:** Frontend (Phase 5), powered by KG data from Phase 2.

### Idea 2: Proactive Case Briefing (Auto-Generated Dashboard)
**What:** When pipeline finishes, automatically generate: key parties, claims→evidence map (with gaps), timeline, conflicting obligations, risk flags. Lawyer opens the case and the briefing is waiting.
**Why it's different:** Every competitor waits for the lawyer to ask. We tell them what matters before they ask.
**Data source:** Checklist agent runs the case-type template automatically after Phase 2 completes.
**Phase:** Phase 4 (agents) + Phase 5 (frontend display).

### Idea 3: Confidence Heatmaps (HITL-Driven Review)
**What:** Show confidence scores visually. High-confidence extractions are solid, low-confidence are faded with "review" badges. Lawyer's attention goes where the AI is least sure.
**Why it's different:** Harvey/Luminance are black boxes. We show where we're guessing.
**Data source:** extraction.confidence, semantic_confidence, classification confidence_score.
**Phase:** Frontend (Phase 5), data already exists.

### Idea 4: Comparative Document View (Side-by-Side Clause Mapping)
**What:** When a complaint alleges breach of a contract clause, show complaint paragraph and contract clause side-by-side with KG edges visually connecting them.
**Why it's different:** No competitor links complaint allegations to contract clauses visually.
**Data source:** exhibit_of edges (Phase 2), supported_by edges (Phase 2), section provenance.
**Phase:** Frontend (Phase 5), powered by cross-doc KG edges.

### Idea 5: Case Law as Graph Extension
**What:** When the case law agent finds relevant precedents, add them as KG nodes with precedent_for / distinguished_by edges. The KG becomes the single convergence point for internal docs + external law.
**Why it's different:** vLex has great case law search but doesn't integrate it into a case-specific KG. Harvey uses precedents for drafting but doesn't map them to specific claims.
**Data source:** Case law agent (Phase 4) writes to kg_nodes + kg_edges.
**Phase:** Phase 4 (case law agent) + Phase 5 (visualization in case map).

### Idea 6: Multi-Case Pattern Detection (Longer Term)
**What:** Aggregate intelligence across cases (anonymized). "In 20 similar breach cases, 73% where termination was triggered pre-breach were dismissed."
**Data source:** Cross-case queries on kg_nodes/extractions (requires many cases).
**Phase:** Post-MVP, requires production usage data.

---

## 4. Pipeline Flow — Full Picture

### Per-Document Pipeline (runs for each uploaded document)
```
Upload → 01_INITIAL (ETL) → 02_MIDDLE (AST + labeling + extraction + intra-doc KG)
```

### Per-Case Pipeline (runs after all docs uploaded, or when new doc added)
```
04B (cross-doc KG) → 05 (graph analytics) → 03_SEARCH (embed sections)
```

### Always-Available Services (query-time, not pipeline)
```
hybrid_search() — called by agents and frontend search bar
kg_traverse() — called by agents for relationship reasoning  
graph_analytics.build_timeline() — called by agents and case briefing
graph_analytics.find_claim_evidence_paths() — called by agents and checklist
```

### Proactive Triggers (after pipeline completes)
```
Case briefing: checklist agent auto-runs the case-type template
Confidence review: flag low-confidence extractions for HITL
Unsupported claims: graph analytics flags claims without evidence paths
```

---

## 5. Data Governance Reminders

- OpenAI embedding API does NOT use API inputs for training (verify before production)
- case_id filter is mandatory — no cross-case search code paths
- Every search result carries provenance (section_id → page_range → anchor_id)
- Confidence < 0.7 → flagged for mandatory human review
- Anonymization option: replace party names with PARTY_A/PARTY_B before embedding for high-sensitivity cases
- For fully local deployment: swap OpenAI embeddings to sentence-transformers (384 dims, change vector column)

---

## 6. Open Questions for Later

- Which LLM for agent reasoning? (GPT-4o, Claude, Gemini — or route different questions to different models)
- Case law data source? (vLex API, CourtListener, Casetext, or scraper)
- Multi-language support? (Spanish Civil Code cross-referencing per competitor analysis)
- Real-time collaboration? (Multiple lawyers working on same case simultaneously)
- Billing integration? (Track time spent reviewing AI outputs vs manual review)