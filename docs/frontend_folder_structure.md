# Frontend File & Folder Structure

## Stack Context

- **Next.js 15** (App Router, `src/` directory)
- **shadcn/ui** (preset `b1GfoPtC4`, Tailwind CSS)
- **Supabase JS client** (auth + realtime + direct queries)
- **Framer Motion** (transitions, panel animations)
- **D3 / react-force-graph** (KG visualization)

Initialized with:
```bash
pnpm dlx shadcn@latest init --preset b1GfoPtC4 --template next
```

---

## The Tree

```
frontend/
├── public/
│   ├── favicon.ico
│   └── logo.svg
│
├── src/
│   ├── app/                                  # Next.js App Router (pages + layouts)
│   │   ├── layout.tsx                        # Root layout: providers, fonts, dark mode
│   │   ├── page.tsx                          # Landing / login redirect
│   │   ├── globals.css                       # Tailwind base + custom CSS vars (glow colors, glassmorphism)
│   │   │
│   │   ├── (auth)/                           # Auth route group (no sidebar)
│   │   │   ├── login/
│   │   │   │   └── page.tsx
│   │   │   └── layout.tsx                    # Minimal layout for auth pages
│   │   │
│   │   ├── (app)/                            # Authenticated route group (has shell)
│   │   │   ├── layout.tsx                    # App shell: CasePulse + PhaseSlider + panel layout
│   │   │   │
│   │   │   ├── dashboard/                    # The "Entryway" — no case selected
│   │   │   │   └── page.tsx                  # OmniDrop zone, recent cases, inbound stream
│   │   │   │
│   │   │   └── case/
│   │   │       └── [caseId]/                 # Dynamic case route — all case views live here
│   │   │           ├── layout.tsx            # Case-scoped layout: loads case metadata, provides CaseContext
│   │   │           ├── page.tsx              # Default case view (redirects to /workspace or /briefing)
│   │   │           │
│   │   │           ├── workspace/            # The main 5-panel workspace
│   │   │           │   └── page.tsx          # KnowledgeShelf + LightBox + LegalPad + Chronology
│   │   │           │
│   │   │           ├── briefing/             # Proactive case briefing (auto-generated dashboard)
│   │   │           │   └── page.tsx          # Parties, claims→evidence, timeline, risk flags
│   │   │           │
│   │   │           ├── board/                # Action Board (Kanban view)
│   │   │           │   └── page.tsx          # Key facts, obligations, missing info, claims, actions
│   │   │           │
│   │   │           ├── map/                  # Living Case Map (KG visualization)
│   │   │           │   └── page.tsx          # Force-directed graph, node detail panel
│   │   │           │
│   │   │           ├── timeline/             # Full-page timeline view (expanded Chronology)
│   │   │           │   └── page.tsx          # Scrollable timeline with time-travel filter
│   │   │           │
│   │   │           ├── compare/              # Comparative document view
│   │   │           │   └── page.tsx          # Side-by-side clause mapping with KG edge connectors
│   │   │           │
│   │   │           └── review/               # HITL review queue (full-page confidence view)
│   │   │               └── page.tsx          # Sorted by confidence, batch verify/dismiss
│   │   │
│   │   └── api/                              # Next.js Route Handlers (BFF layer)
│   │       ├── search/
│   │       │   └── route.ts                  # POST → proxies to backend 03_SEARCH/03_search_api.py
│   │       ├── query/
│   │       │   └── route.ts                  # POST → proxies to backend 04_AGENTS/query_handler.py
│   │       ├── ingest/
│   │       │   └── route.ts                  # POST → triggers Phase 1 pipeline (file upload)
│   │       ├── reviews/
│   │       │   └── route.ts                  # POST/PATCH → writes to reviews table (HITL)
│   │       └── case/
│   │           └── [caseId]/
│   │               ├── briefing/
│   │               │   └── route.ts          # GET → checklist agent output
│   │               ├── timeline/
│   │               │   └── route.ts          # GET → graph_analytics.build_timeline()
│   │               ├── graph/
│   │               │   └── route.ts          # GET → kg_nodes + kg_edges for visualization
│   │               ├── entities/
│   │               │   └── route.ts          # GET → kg_nodes filtered by type (for Knowledge Shelf)
│   │               └── documents/
│   │                   └── route.ts          # GET → documents list for case
│   │
│   ├── components/                           # All React components
│   │   │
│   │   ├── shell/                            # App shell — always visible when authenticated
│   │   │   ├── case-pulse.tsx                # Top bar: matter #, active claim, deadline countdown, pipeline status
│   │   │   ├── phase-slider.tsx              # Triage / Discovery / Trial Prep toggle
│   │   │   ├── command-bar.tsx               # ⌘K global search + navigation + actions
│   │   │   └── panel-layout.tsx              # Resizable 3-column layout manager (Knowledge Shelf | LightBox | Legal Pad)
│   │   │
│   │   ├── entryway/                         # Dashboard / landing components
│   │   │   ├── omni-drop-zone.tsx            # Central drag-and-drop file ingestion area
│   │   │   ├── recent-cases.tsx              # Case cards with pulse data
│   │   │   └── inbound-stream.tsx            # Pending ingestions from WhatsApp, email, integrations
│   │   │
│   │   ├── knowledge-shelf/                  # Left sidebar components
│   │   │   ├── knowledge-shelf.tsx           # Container: entity list + inbound stream
│   │   │   ├── entity-card.tsx               # Single entity card (person/company/contract/claim)
│   │   │   ├── entity-card-draggable.tsx     # Drag wrapper for entity cards (drop onto viewer/pad)
│   │   │   └── entity-filter.tsx             # Filter/search within entities by type, name, confidence
│   │   │
│   │   ├── lightbox/                         # Center document viewer components
│   │   │   ├── lightbox-viewer.tsx           # Main XHTML/HTML document renderer
│   │   │   ├── document-highlighter.tsx      # Applies glow overlays (green/amber/red) based on extraction confidence
│   │   │   ├── margin-scribble.tsx           # AI sticky note in the right margin (reasoning chain)
│   │   │   ├── hover-sync-handler.tsx        # Listens for hover events from Legal Pad, scrolls + highlights
│   │   │   ├── comparative-view.tsx          # Split-pane side-by-side document view
│   │   │   ├── kg-edge-connector.tsx         # Visual lines between complaint paragraph ↔ contract clause
│   │   │   └── document-nav.tsx              # TOC sidebar / breadcrumb within the viewer
│   │   │
│   │   ├── legal-pad/                        # Right panel — chat + HITL verification
│   │   │   ├── legal-pad.tsx                 # Container: chat on top, verification queue below
│   │   │   ├── chat-pane.tsx                 # Bubble-style conversational chat
│   │   │   ├── chat-message.tsx              # Single message bubble (user or AI)
│   │   │   ├── chat-actions.tsx              # Action buttons below AI responses: Pin to Case, Add to Timeline, etc.
│   │   │   ├── pin-to-case-handler.tsx       # Logic: extracts chat response → creates KG entry
│   │   │   ├── why-toggle.tsx                # Expandable reasoning chain for any AI suggestion
│   │   │   ├── verification-queue.tsx        # List of draft facts awaiting HITL confirm/dismiss
│   │   │   ├── verification-card.tsx         # Single fact card: text, confidence bar, anchor, ✓/✗ buttons
│   │   │   └── ai-question-prompt.tsx        # "I found a discrepancy..." — AI asks the lawyer a question
│   │   │
│   │   ├── chronology/                       # Bottom timeline drawer
│   │   │   ├── chronology-drawer.tsx         # Collapsible bottom tray container
│   │   │   ├── timeline-strip.tsx            # Horizontal scrollable timeline with event dots
│   │   │   ├── timeline-dot.tsx              # Single event dot (color-coded by doc type, tooltip on hover)
│   │   │   └── time-travel-filter.tsx        # Date picker that filters entire UI to a point in time
│   │   │
│   │   ├── action-board/                     # Kanban view components
│   │   │   ├── action-board.tsx              # Kanban container with columns
│   │   │   ├── board-column.tsx              # Single column (Key Facts, Obligations, Missing Info, etc.)
│   │   │   └── board-card.tsx                # Draggable card: item text, linked proof, status, confidence
│   │   │
│   │   ├── case-map/                         # Living Case Map (KG visualization)
│   │   │   ├── case-map.tsx                  # Force-directed graph container (D3 or react-force-graph)
│   │   │   ├── graph-node.tsx                # Custom node renderer (party/document/claim, sized by involvement)
│   │   │   ├── graph-edge.tsx                # Custom edge renderer (thickness = evidence strength, red = unsupported)
│   │   │   └── node-detail-panel.tsx         # Side panel when clicking a node: provenance, linked sections
│   │   │
│   │   ├── briefing/                         # Auto-generated case briefing components
│   │   │   ├── briefing-dashboard.tsx        # Layout: parties + claims + timeline + risks
│   │   │   ├── parties-summary.tsx           # Identified parties with confidence badges
│   │   │   ├── claims-evidence-map.tsx       # Each claim → evidence paths (with gap flags)
│   │   │   ├── risk-flags.tsx                # Conflicting obligations, unsupported claims
│   │   │   └── briefing-timeline.tsx         # Compact auto-timeline embedded in briefing
│   │   │
│   │   ├── review/                           # HITL review queue components
│   │   │   ├── review-queue.tsx              # Full-page review: sorted by confidence
│   │   │   ├── review-item.tsx              # Expandable extraction with source context
│   │   │   └── confidence-heatmap.tsx        # Visual confidence indicators (solid/faded/badge)
│   │   │
│   │   └── ui/                               # shadcn/ui primitives (auto-generated by CLI)
│   │       ├── button.tsx
│   │       ├── card.tsx
│   │       ├── dialog.tsx
│   │       ├── dropdown-menu.tsx
│   │       ├── input.tsx
│   │       ├── popover.tsx
│   │       ├── separator.tsx
│   │       ├── sheet.tsx                     # Used for mobile Knowledge Shelf / Legal Pad
│   │       ├── skeleton.tsx
│   │       ├── slider.tsx
│   │       ├── tabs.tsx
│   │       ├── toast.tsx
│   │       ├── tooltip.tsx
│   │       └── ...                           # Other shadcn components as needed
│   │
│   ├── hooks/                                # Custom React hooks
│   │   ├── use-case.ts                       # Current case context (caseId, metadata, phase)
│   │   ├── use-agent-query.ts                # Send query to agent router, handle streaming response
│   │   ├── use-search.ts                     # Hybrid search with debounce + filter management
│   │   ├── use-entities.ts                   # Fetch + subscribe to KG entities for Knowledge Shelf
│   │   ├── use-timeline.ts                   # Fetch timeline data, handle time-travel filtering
│   │   ├── use-graph.ts                      # Fetch KG nodes + edges for Case Map
│   │   ├── use-briefing.ts                   # Fetch auto-generated case briefing
│   │   ├── use-review-queue.ts               # Fetch pending HITL items, submit verify/dismiss
│   │   ├── use-realtime.ts                   # Supabase Realtime subscription manager
│   │   ├── use-hover-sync.ts                 # Shared hover state between Legal Pad ↔ LightBox
│   │   ├── use-panel-layout.ts               # Panel resize/collapse state management
│   │   ├── use-phase.ts                      # Current phase (Triage/Discovery/TrialPrep) + UI weight config
│   │   └── use-file-upload.ts                # Drag-and-drop + ingestion pipeline trigger
│   │
│   ├── lib/                                  # Shared utilities and client setup
│   │   ├── supabase/
│   │   │   ├── client.ts                     # Browser Supabase client (anon key, auth)
│   │   │   ├── server.ts                     # Server-side Supabase client (service role, for Route Handlers)
│   │   │   ├── middleware.ts                 # Auth middleware for protected routes
│   │   │   └── realtime.ts                   # Realtime channel helpers (subscribe to table changes)
│   │   │
│   │   ├── api/
│   │   │   ├── search.ts                     # fetch wrapper for POST /api/search
│   │   │   ├── query.ts                      # fetch wrapper for POST /api/query (agent)
│   │   │   ├── ingest.ts                     # fetch wrapper for POST /api/ingest (file upload)
│   │   │   ├── reviews.ts                    # fetch wrapper for POST/PATCH /api/reviews
│   │   │   ├── briefing.ts                   # fetch wrapper for GET /api/case/:id/briefing
│   │   │   ├── timeline.ts                   # fetch wrapper for GET /api/case/:id/timeline
│   │   │   ├── graph.ts                      # fetch wrapper for GET /api/case/:id/graph
│   │   │   └── entities.ts                   # fetch wrapper for GET /api/case/:id/entities
│   │   │
│   │   ├── types/                            # TypeScript types mirroring backend schemas
│   │   │   ├── case.ts                       # Case, Document metadata
│   │   │   ├── section.ts                    # Section with AST hierarchy, page_range, semantic_label
│   │   │   ├── extraction.ts                 # Typed extraction (party, claim, obligation, date, amount, etc.)
│   │   │   ├── kg.ts                         # KGNode, KGEdge (mirrors kg_nodes/kg_edges tables)
│   │   │   ├── agent-response.ts             # AgentResponse: answer, confidence, provenance_links, needs_review
│   │   │   ├── search-result.ts              # SearchResult with scores, provenance
│   │   │   ├── timeline-event.ts             # TimelineEvent from build_timeline()
│   │   │   ├── review.ts                     # Review (HITL confirm/dismiss records)
│   │   │   └── checklist.ts                  # ChecklistTask, ChecklistTemplate
│   │   │
│   │   ├── constants/
│   │   │   ├── phases.ts                     # Phase enum + UI weight configs per phase
│   │   │   ├── glow-colors.ts                # Highlight colors: verified, unverified, contradicts, low-confidence
│   │   │   ├── entity-types.ts               # party, company, contract, claim — icons + colors
│   │   │   └── doc-type-colors.ts            # Color mapping for document types (timeline dots, board cards)
│   │   │
│   │   └── utils/
│   │       ├── confidence.ts                 # Confidence → visual weight (opacity, badge, glow)
│   │       ├── provenance.ts                 # Build anchor links from provenance_links
│   │       ├── format-date.ts                # Timeline date formatting (relative, absolute, "approx.")
│   │       └── cn.ts                         # Tailwind class merge utility (from shadcn)
│   │
│   └── providers/                            # React context providers
│       ├── case-provider.tsx                  # CaseContext: current case data, active phase, entities
│       ├── chat-provider.tsx                  # ChatContext: conversation history, streaming state
│       ├── hover-sync-provider.tsx            # HoverSyncContext: shared hover target between panels
│       ├── realtime-provider.tsx              # RealtimeContext: manages Supabase Realtime subscriptions
│       └── theme-provider.tsx                 # Dark/light mode (next-themes)
│
├── .env.local                                # NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY, BACKEND_URL
├── components.json                           # shadcn/ui config (generated by init)
├── middleware.ts                              # Next.js middleware: auth guard, redirect unauthenticated
├── next.config.ts
├── package.json
├── postcss.config.mjs
├── tailwind.config.ts                        # Theme extensions: glassmorphism, glow utilities, custom colors
└── tsconfig.json
```

---

## Mapping: Components → Backend Data Sources

This table shows which backend endpoint or Supabase table each major component reads from. This is important because it tells you exactly what data contract each component depends on — if you rename a column or change an API response shape, you know what breaks.

| Component | Backend Source | Data Shape |
|---|---|---|
| `case-pulse.tsx` | Supabase `cases` + `documents` tables | case metadata, next_deadline, pipeline_status |
| `phase-slider.tsx` | Local state (URL param `?phase=triage`) | — |
| `command-bar.tsx` | `POST /api/search` + `POST /api/query` | SearchResult[], AgentResponse |
| `omni-drop-zone.tsx` | `POST /api/ingest` | triggers pipeline, returns document_id |
| `entity-card.tsx` | `GET /api/case/:id/entities` | KGNode[] filtered by type |
| `lightbox-viewer.tsx` | Supabase `sections` table (section_text, tagged XHTML) | Section with HTML content |
| `document-highlighter.tsx` | Supabase `extractions` (confidence per section) | Extraction[] with confidence float |
| `margin-scribble.tsx` | `agent_responses` table (reasoning_steps) | string[] reasoning chains |
| `chat-pane.tsx` | `POST /api/query` (streaming) | AgentResponse (streamed) |
| `pin-to-case-handler.tsx` | `POST /api/query` → writes to `kg_nodes` + `kg_edges` | creates KGNode + KGEdge |
| `verification-queue.tsx` | Supabase `extractions` WHERE confidence < 0.7 | Extraction[] sorted by confidence |
| `verification-card.tsx` | `POST /api/reviews` | creates Review record |
| `timeline-strip.tsx` | `GET /api/case/:id/timeline` | TimelineEvent[] from build_timeline() |
| `case-map.tsx` | `GET /api/case/:id/graph` | KGNode[] + KGEdge[] (full graph) |
| `briefing-dashboard.tsx` | `GET /api/case/:id/briefing` | ChecklistTask[] with agent results |
| `claims-evidence-map.tsx` | `GET /api/case/:id/graph` → client-side BFS | claim→evidence paths |
| `comparative-view.tsx` | Supabase `sections` × 2 + `kg_edges` (exhibit_of, supported_by) | two Section objects + edges |
| `review-queue.tsx` | Supabase `extractions` + `reviews` | Extraction[] with review status |
| `confidence-heatmap.tsx` | Supabase `extractions` (confidence field) | aggregated confidence per section |

---

## Mapping: Components → UI Architecture Plan Sections

| UI Architecture Plan Section | Primary Component(s) |
|---|---|
| §2A Case Pulse | `shell/case-pulse.tsx` |
| §2B Phase Slider | `shell/phase-slider.tsx` |
| §2C Command Bar | `shell/command-bar.tsx` |
| §3A Knowledge Shelf | `knowledge-shelf/*` |
| §3B Light-Box Viewer | `lightbox/*` |
| §3C Legal Pad | `legal-pad/*` |
| §3D Chronology Drawer | `chronology/*` |
| §3E Action Board | `action-board/*` |
| §4 Entryway | `entryway/*` + `dashboard/page.tsx` |
| §5A Living Case Map | `case-map/*` + `case/[caseId]/map/page.tsx` |
| §5B Proactive Briefing | `briefing/*` + `case/[caseId]/briefing/page.tsx` |
| §5C Confidence Heatmaps | `review/*` + `lightbox/document-highlighter.tsx` |
| §5D Comparative View | `lightbox/comparative-view.tsx` + `case/[caseId]/compare/page.tsx` |
| §5E Case Law Graph | `case-map/*` (precedent nodes rendered same as other KG nodes) |

---

## Notes on Architecture Decisions

**Why a BFF layer (Next.js Route Handlers) instead of calling backend directly?**

Your backend is Python (FastAPI). Your frontend is Next.js. The Route Handlers in `src/app/api/` act as a Backend-for-Frontend proxy. This gives you three things: (1) the Supabase service role key stays server-side and never hits the browser, (2) you can reshape backend responses to match exactly what the component needs without over-fetching, and (3) you have a single place to add auth checks, rate limiting, and request validation before anything touches the Python backend.

**Why one `types/` folder mirroring backend schemas?**

Your backend has `schemas/response.py` with the `AgentResponse` Pydantic model. The `ProvenanceLink`, `AgentResponse`, and other shapes need TypeScript equivalents. Keeping them in `lib/types/` as a single source of truth means when you change the backend schema, you update one place in the frontend. If you later add code generation (e.g., `openapi-typescript`), this folder is where the output goes.

**Why context providers instead of a global store?**

The data in this app is heavily scoped. `CaseContext` only matters inside `case/[caseId]/`. `ChatContext` only matters inside the Legal Pad. `HoverSyncContext` only matters when the Light-Box and Legal Pad are both mounted. React context providers compose cleanly with the App Router layout hierarchy — each layout injects the provider for its scope, and child pages get the data they need without prop drilling or a monolithic store. If performance becomes an issue (e.g., the KG graph is huge), you can swap individual providers to Zustand stores without changing the component API.

**Why `hooks/use-realtime.ts`?**

When the pipeline finishes processing a document, the Knowledge Shelf needs to update its entity cards. When a collaborator verifies a fact, the verification queue needs to reflect it. Supabase Realtime subscriptions handle this. The `use-realtime` hook manages channel subscriptions with automatic cleanup on unmount, and the `realtime-provider` holds shared channel references so multiple components can subscribe to the same table without creating duplicate connections.