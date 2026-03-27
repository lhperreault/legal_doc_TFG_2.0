# UI Architecture Plan — Phase 5

## 1. Design Philosophy

The core idea across all your notes is one principle: **the UI is not a database the lawyer maintains — it's an intelligence the lawyer talks to, which maintains the database for them.** Every design decision should pass this test. If the lawyer has to manually organize, file, or cross-reference something, the UI has failed.

Secondary principles drawn from your notes:

- **Calm by default, rich on demand.** Information appears when summoned (via chat, hover, click), not dumped on screen. The "Professional Desktop" vibe — dark mode, Inter/SF Pro, generous whitespace, glassmorphism accents.
- **Conversation becomes infrastructure.** Chat isn't a throwaway stream. Every AI response is a potential entry in the knowledge graph, timeline, or action board. The "Pin to Case" pattern is central.
- **Provenance everywhere.** Every fact, suggestion, and extraction traces back to a specific page, paragraph, and source document. Hover-sync and anchor links are not nice-to-haves — they're the trust layer.
- **Confidence is visible.** The system tells the lawyer where it's guessing. Faded cards, review badges, glow colors. The opposite of a black box.

---

## 2. Application Shell & Navigation

### 2A. Case Pulse (Top Bar)

A thin, translucent glassmorphism strip. Always visible. Contains:

- **Matter number + case name** (left)
- **Active claim / current phase** (center) — e.g., "Breach of Contract — Discovery"
- **Next deadline countdown** (right) — live ticker, "14d 6h to Motion Deadline"
- **Pipeline status indicator** — subtle dot: green (all docs processed), amber (pipeline running), red (extraction errors need review)

This is the lawyer's grounding element. Like a watch face — glance and know where you stand.

### 2B. Phase & Task Slider

Horizontal toggle immediately below the Case Pulse. Three modes that reshape the entire UI emphasis:

| Mode | UI Emphasis | What Surfaces |
|---|---|---|
| **Triage** | Extraction, parties, dates, the "story" | Drop zone prominent, entity cards, auto-briefing |
| **Discovery** | Production tracking, request/response pairing | Document lists, comparative view, evidence gaps |
| **Trial Prep** | Contradictions, evidence strength, case map | KG visualization, confidence heatmaps, timeline |

Switching mode doesn't destroy context — it re-weights which panels are expanded vs. collapsed and which filters are pre-applied.

### 2C. Command Bar (⌘K / Ctrl+K)

Global, always accessible. Accepts:

- Natural language questions: "What was the last thing we got from the CEO?"
- Entity lookups: "Luke Perreault" → shows entity card + related docs
- Navigation: "Go to MSA Section 3.8"
- Actions: "Draft response to opposing counsel re: late discovery"

This is the primary input method. The command bar routes to the query handler / router (Phase 4 backend), which classifies intent and dispatches to the right agent.

---

## 3. The Five Core Panels

The workspace is a flexible multi-panel layout. All panels can be resized, collapsed, or rearranged. Default layout:

```
┌─────────────────────────────────────────────────────┐
│                   CASE PULSE (top bar)               │
├─────────────────────────────────────────────────────┤
│            PHASE SLIDER: [Triage] [Discovery] [Prep]│
├──────────┬───────────────────────┬──────────────────┤
│          │                       │                  │
│ KNOWLEDGE│     LIGHT-BOX         │   LEGAL PAD      │
│  SHELF   │     VIEWER            │   (HITL +        │
│ (sidebar)│     (center stage)    │    Chat)          │
│          │                       │                  │
├──────────┴───────────────────────┴──────────────────┤
│              CHRONOLOGY DRAWER (collapsed)           │
└─────────────────────────────────────────────────────┘
```

### 3A. Knowledge Shelf (Left Sidebar)

The lawyer's filing cabinet, reimagined as interactive entity cards.

**Entity Cards** — small, swipeable cards for every Person, Company, Contract, and Claim in the case. Each card shows:

- Entity name + type icon
- Relationship count (how many KG edges)
- Confidence indicator (solid = high, faded = needs review)
- Quick-action: drag onto the Light-Box Viewer to highlight all mentions of that entity in the current document

**Drag interactions:**

- Drag entity card → Light-Box Viewer: highlights every mention, shows KG relationship to current doc
- Drag entity card → Legal Pad: starts a focused chat about that entity ("Tell me everything we know about Party X")
- Drag entity card → another entity card: shows the KG edge between them (obligations, claims, etc.)

**Inbound Stream** — a subsection showing "Pending Ingestions" from connected sources (email, WhatsApp, file drops). Each shows a snippet preview. Flick into the case to activate the ingestion pipeline.

**Data source:** `kg_nodes` (entities), `extractions` (typed facts), `documents` (file metadata)

### 3B. Light-Box Viewer (Center Stage)

The tagged XHTML/HTML document reader. This is where the lawyer reads the actual source material.

**Core features:**

- Clean, paper-white panel with generous margins (even in dark mode — the document itself stays light for readability)
- Smooth scroll and glide transitions (Framer Motion) when navigating via TOC or anchor links
- **Digital Highlighter** — AI-applied color coding on extracted facts:
  - Green glow: verified fact (confirmed by signature, cross-referenced)
  - Amber glow: extracted but unverified (awaiting HITL confirmation)
  - Red glow: contradicts other evidence in the case
  - Faded/dotted outline: low-confidence extraction (< 0.7 threshold)
- **Margin Scribbles** — right-margin sticky notes from the AI explaining why a section was tagged. Shows the reasoning chain: "Tagged because Section 8.2 says X, and WhatsApp message #042 says Y."
- **Hover-Sync** — when the lawyer hovers over any AI suggestion in the Legal Pad, the viewer auto-scrolls to and highlights the source paragraph

**Comparative mode** (activated from Discovery phase or via command):

- Split-pane showing two documents side-by-side
- KG edges rendered as visual connectors between complaint paragraph ↔ contract clause
- Uses `exhibit_of` and `supported_by` edges from the cross-doc KG

**Data source:** tagged XHTML from Phase 1, `sections` table for structure, `kg_edges` for cross-doc linking

### 3C. Legal Pad (Right Panel — HITL + Chat)

This is the conversational workspace AND the human verification layer. It serves two merged functions:

**Chat mode (top portion):**

- Familiar bubble-style chat
- Every AI response includes:
  - **Pin to Case** button — extracts the response into the KG (e.g., pin a payment summary → creates entry under Obligations > Payments > Late Fees)
  - **Actionable Next Steps** — suggested buttons at the bottom of each turn:
    - [Add to Timeline]
    - [Draft Response Email]
    - [Flag for Partner Review]
    - [Link to Exhibit]
  - **"Why" Toggle** — expands the reasoning chain for any AI suggestion

**Verification mode (bottom portion):**

- List of "Draft Facts" the AI has extracted but not yet confirmed
- Each fact shows:
  - The extracted text
  - Confidence score (visual bar)
  - Anchor icon → click to glide the Light-Box Viewer to the proof
  - ✓ Verify / ✗ Dismiss buttons
- AI questions appear here: "I found a 12% discrepancy in the Q3 Invoice. Should I add this to the Breach of Contract claim?"

**Verification workflow:**

1. AI proposes: "I found a signature on Page 12 that matches the Defendant."
2. Lawyer hovers → Light-Box scrolls to Page 12, highlights the signature
3. Lawyer clicks ✓ Confirm
4. KG updates: "Defendant → Signed → Contract" is cemented
5. Card pulses briefly in the Knowledge Shelf to signal the update

**Data source:** Agent responses (Phase 4 `agent_responses` table), `reviews` table for HITL corrections, `kg_nodes` + `kg_edges` for graph updates

### 3D. Chronology Drawer (Bottom Tray)

Collapsed by default. Pull up to expand. A horizontal scrollable timeline.

- Every email, contract signature, payment, filing, and key event is a dot on the line
- Dots are color-coded by document type (contract = blue, communication = green, pleading = red, financial = amber)
- Clicking a point updates the entire UI to show contextual state at that moment — which documents existed, what the KG looked like, what was known vs. unknown
- Hovering a dot shows a tooltip with the event summary and linked document

**Time-travel feature (stretch goal):** "What did we know on June 12, 2024?" — the UI filters everything to that point in time, showing only documents and KG state that existed before that date.

**Data source:** `graph_analytics.build_timeline()`, `extractions` (dates), `documents` (upload timestamps, document dates)

### 3E. Action Board (Kanban View)

Accessible as an alternative to the default panel layout (tab or toggle). A dynamic kanban board populated by pinned chat items and AI extractions.

| Column | What Goes Here |
|---|---|
| **Key Facts** | Verified facts from HITL review |
| **Obligations** | Contract obligations with due dates and status |
| **Missing Info** | Gaps flagged by the AI (missing exhibits, unlinked evidence) |
| **Claims** | Each cause of action with evidence strength indicator |
| **Action Items** | Tasks generated from chat (draft email, request document, flag for review) |

Each card shows: the item text, linked proof (document + page), status (verified/pending/overdue), and confidence score.

**Data source:** Populated from `kg_nodes` (type-filtered), `extractions`, and agent responses that were "pinned" via the Legal Pad

---

## 4. The Entryway (New Case / Landing View)

When no case is open, or when starting fresh, the lawyer sees a minimal "Command Center":

**Omni-Drop Zone** — a large central glassmorphism area. Drag any file to start ingestion. Supports drag from desktop, email attachments, and connected integrations.

**Recent Cases** — cards showing the 3-5 most recent cases with their Case Pulse data (next deadline, pipeline status, last activity).

**Inbound Stream** — pending items from WhatsApp, email, and other integrations that haven't been assigned to a case yet.

**Global Search** — the ⌘K bar, scoped across all cases (with permission filters).

---

## 5. Agentic Intelligence Features

These are the differentiators — features that leverage the backend pipeline, KG, and agent architecture in ways competitors don't.

### 5A. Living Case Map (Interactive KG Visualization)

A force-directed graph showing the entire case structure. Not decoration — a navigation tool.

- Parties as nodes (sized by involvement)
- Documents as colored regions/clusters around parties
- Claims as edges connecting parties to documents
- Edge thickness = evidence strength (from `find_claim_evidence_paths`)
- Unsupported claims glow red
- Click a node → side panel shows details with full provenance chain
- Zoom into a cluster → expands to show individual sections and extractions

**Lawyer value:** Glance at the map and immediately see "this claim has no evidence trail" or "these two contracts reference the same party but with different obligations."

**Data source:** `kg_nodes`, `kg_edges`, `graph_analytics` (unsupported claims, evidence paths)

### 5B. Proactive Case Briefing (Auto-Generated Dashboard)

When the pipeline finishes processing a case, the system doesn't wait for the lawyer to ask questions. It runs the case-type checklist template automatically and generates a briefing:

- Key parties identified (with confidence)
- Claims mapped to supporting evidence (with gaps flagged)
- Timeline of events (auto-generated)
- Conflicting obligations detected across documents
- Risk flags and missing exhibits
- Estimated evidence strength per claim

The lawyer opens the case and the briefing is already waiting. This becomes the "home screen" for the case within the Triage phase.

**Data source:** Checklist agent runs the case-type template (e.g., `breach_of_contract.json`) automatically after Phase 2 completes. Results stored in `agent_responses`.

### 5C. Confidence Heatmaps (HITL-Driven Review)

Every extraction carries a confidence score. Instead of hiding it, make it the primary visual signal for where to focus human review.

- High-confidence extractions render as solid cards/highlights
- Low-confidence extractions render as faded with a "Review" badge
- A dedicated "Review Queue" view sorts all pending items by confidence (lowest first)
- After the lawyer confirms or corrects, the confidence data feeds back into extraction quality metrics

**Lawyer value:** Attention goes exactly where the AI is least sure — maximum value from human review time.

**Data source:** `extraction.confidence`, `semantic_confidence`, classification `confidence_score` — all already in the database

### 5D. Comparative Document View (Side-by-Side Clause Mapping)

When a complaint alleges breach of a specific contract clause:

- Left pane: the complaint paragraph making the allegation
- Right pane: the actual contract clause being referenced
- Visual connectors between them (powered by `exhibit_of` and `supported_by` KG edges)
- KG edge metadata displayed: relationship type, confidence, extraction source

**Lawyer value:** See exactly what's alleged and what the contract actually says, in one glance. No tabbing between documents.

**Data source:** Cross-doc KG edges from Phase 2, `sections` table for source text

### 5E. Case Law as Graph Extension

When the case law agent finds relevant precedents, they don't just appear in a list. They get added to the KG as first-class nodes:

- Precedent node: "Smith v. Jones (2019) — held that 30% commission is not anticompetitive"
- Connected to relevant claims via `precedent_for` or `distinguished_by` edges
- Visible in the Living Case Map alongside internal documents

**Lawyer value:** One place to see everything — "our breach claim is supported by Exhibit A AND backed by two favorable precedents AND one unfavorable one."

**Data source:** Case law agent (Phase 4) writes to `kg_nodes` + `kg_edges`

### 5F. Multi-Case Pattern Detection (Post-MVP)

Aggregate intelligence across cases (anonymized). "In 20 similar breach cases, 73% where termination was triggered pre-breach were dismissed."

**Data source:** Cross-case queries on `kg_nodes`/`extractions`. Requires production usage data.

---

## 6. New Ideas (Not in Original Notes)

### 6A. "Ghost Draft" — AI-Prepared Document Drafts

When the AI identifies a pattern that typically requires a response (e.g., a discovery request, a motion response), it should prepare a ghost draft proactively. The lawyer sees a notification: "I've prepared a draft response to the discovery request based on the case facts. Review?" The draft is pre-populated with cited facts from the KG and linked to source documents.

**Why:** Shifts the lawyer's role from "write from scratch" to "edit and approve." Saves hours on routine drafting.

**Backend hook:** New agent type (`drafting_agent`) that uses the same shared tools + a document template library. Returns structured output with provenance like every other agent.

### 6B. "Red Team" Mode — Adversarial Analysis

A toggle (or phase mode) where the AI argues the opposing side's case. It takes all the evidence and extractions and constructs the strongest possible counter-arguments. Surfaces weaknesses the lawyer might miss.

- "If I were opposing counsel, I would argue that the 30-day cure period in Section 5.1 was never properly triggered because..."
- Each counter-argument linked to the specific evidence or gap it exploits

**Why:** Lawyers do this mentally anyway. Having it structured and evidence-linked saves time and catches blind spots.

**Backend hook:** Same complaint/contract agents, but with an adversarial system prompt. The provenance chain still works — it just traces to weaknesses instead of strengths.

### 6C. Session Replay & Audit Trail

Every interaction — chat queries, HITL confirmations, pin-to-case actions, document views — is logged with timestamps. Two uses:

1. **Billing integration:** Track time spent per case, per document, per activity type. Auto-generate time entries.
2. **Audit trail:** For regulatory or court purposes, show exactly how a conclusion was reached: "On March 3, the AI extracted this fact → on March 5, the lawyer verified it → on March 8, it was cited in the motion."

**Data source:** `agent_responses` table already stores AI outputs. Add a `user_actions` table for clicks, verifications, and navigation events.

### 6D. "Watchdog" Alerts — Deadline & Change Monitoring

The system monitors for time-sensitive conditions and sends proactive alerts:

- Obligation deadlines approaching (from extraction data)
- New document uploaded that contradicts a verified fact (KG conflict detection)
- Opposing counsel filing detected (if connected to court filing feeds)
- Confidence score drops after re-processing (e.g., new evidence invalidates a previously strong extraction)

**Why:** Lawyers miss deadlines. This is the system watching their back.

**Backend hook:** Scheduled jobs that run `graph_analytics` checks + extraction deadline queries. Push notifications via the Inbound Stream.

### 6E. Collaborative Annotations

Multiple lawyers working on the same case can leave annotations on documents and KG nodes. These are distinct from AI-generated margin scribbles — they're human notes, visible to the team, threaded like comments.

- Annotations appear as a different color in the Light-Box margin (e.g., blue for human, gray for AI)
- Taggable: @mention a colleague to flag something for their review
- Annotation history preserved for audit

**Backend hook:** New `annotations` table linked to `section_id` + `user_id`. Real-time sync via Supabase Realtime subscriptions.

### 6F. Smart Intake Routing

When a file is dropped into the Omni-Drop Zone, the system doesn't just process it — it classifies it and suggests routing:

- "This looks like a Complaint. Should I add it to Case #2024-0847 (Acme v. GlobalCorp) or create a new case?"
- "This PDF contains 3 exhibits embedded as attachments. Should I split them into separate documents?"
- "This appears to be a duplicate of Document #12 (uploaded March 1). Merge or keep separate?"

**Backend hook:** Uses `05_doc_classification.py` (already built) + duplicate detection via embedding similarity on the first section.

---

## 7. Tech Stack Notes

### Frontend

- **Next.js** (App Router) — SSR for initial load, client for interactive panels
- **shadcn/ui** — component library base (you've already chosen the preset: `pnpm dlx shadcn@latest init --preset b1GfoPtC4 --template next`)
- **Framer Motion** — transitions, glide animations, panel resize, card pulse effects
- **D3.js or react-force-graph** — for the Living Case Map (force-directed KG visualization)
- **Tailwind CSS** — utility styling, dark mode theming
- **Supabase Realtime** — live updates when pipeline completes, new extractions arrive, or collaborators annotate

### Backend API Surface

The frontend talks to these endpoints (Phase 3 search API + Phase 4 agent API):

| Endpoint | Backend | Purpose |
|---|---|---|
| `POST /api/search` | `03_SEARCH/03_search_api.py` | Hybrid search with filters |
| `POST /api/query` | `04_AGENTS/query_handler.py` | Conversational agent queries (routed to specialized agents) |
| `GET /api/case/:id/briefing` | Checklist agent output | Auto-generated case dashboard |
| `GET /api/case/:id/timeline` | `graph_analytics.build_timeline()` | Chronology data |
| `GET /api/case/:id/graph` | `kg_nodes` + `kg_edges` | KG data for visualization |
| `POST /api/reviews` | `reviews` table | HITL confirmations/corrections |
| `GET /api/case/:id/entities` | `kg_nodes` filtered | Entity cards for Knowledge Shelf |
| `POST /api/ingest` | Phase 1 pipeline trigger | File upload → pipeline |

### State Management

- Agent conversation state: LangGraph checkpointer (Supabase-backed for production)
- UI panel state: local React state + URL params (so panel layouts are shareable/bookmarkable)
- Real-time updates: Supabase Realtime subscriptions on `agent_responses`, `kg_nodes`, `documents`

---

## 8. Build Priority (Suggested Sequence)

### Tier 1 — The Core Loop (MVP)

These features make the product usable for a single lawyer on a single case.

1. **Case Pulse + Phase Slider** — navigation shell
2. **Light-Box Viewer** — document reading with basic highlighting (green/amber/red glows from extraction confidence)
3. **Legal Pad — Chat mode** — conversational queries via the query handler + complaint agent
4. **⌘K Command Bar** — global search and navigation
5. **Omni-Drop Zone** — file upload triggering the pipeline

### Tier 2 — The Intelligence Layer

These make the product smarter than a document reader.

6. **Legal Pad — Verification mode** — HITL confirm/dismiss workflow
7. **Knowledge Shelf** — entity cards with drag interactions
8. **Proactive Case Briefing** — auto-dashboard after pipeline completion
9. **Pin to Case** — chat responses → KG entries
10. **Confidence Heatmaps** — visual confidence on all extractions

### Tier 3 — The Differentiators

These are what competitors can't do.

11. **Living Case Map** — force-directed KG visualization
12. **Comparative Document View** — side-by-side clause mapping
13. **Chronology Drawer** — interactive timeline with time-travel
14. **Action Board (Kanban)** — dynamic case task management
15. **Hover-Sync + Margin Scribbles** — deep provenance UX

### Tier 4 — Platform Features

These make it a team product and a business.

16. **Collaborative Annotations** — multi-user notes
17. **Ghost Drafts** — AI-prepared document drafts
18. **Red Team Mode** — adversarial analysis
19. **Watchdog Alerts** — deadline + change monitoring
20. **Session Replay / Audit Trail** — billing + compliance
21. **Case Law Graph Extension** — precedent integration
22. **Smart Intake Routing** — intelligent file classification on drop
23. **Multi-Case Pattern Detection** — cross-case analytics