# Phase 3: Vector Storage & RAG Search — Complete Planning Document

## 1. What You Have (Input from Phases 1 & 2)

### 1.1 Supabase `sections` Table (Primary Embedding Source)

Each section is a semantically coherent chunk — your natural embedding unit. After Phase 2, each row has:

| Column | What It Gives You |
|---|---|
| `id` | UUID primary key — becomes the vector's foreign key |
| `document_id` | FK to documents table — join to get case_id, document_type |
| `section_title` | Heading text. Prepended to section_text before embedding for context. |
| `section_text` | **Full extracted text. This is what gets embedded.** |
| `level` | Hierarchy depth (0 = top). Used as a metadata filter. |
| `page_range` | Provenance. Returned with search results so the frontend can link back. |
| `start_page` / `end_page` | Numeric page bounds for sorting and display. |
| `is_synthetic` | Boolean. Synthetic headings get lower weight in keyword search. |
| `anchor_id` | HTML element ID for frontend deep-linking (HTML docs only). |
| `parent_section_id` | FK to parent section. Enables "show me the parent context" in results. |
| `semantic_label` | Ontology label (e.g., "obligation.payment", "causes_of_action.fraud"). **Primary structural filter.** |
| `semantic_confidence` | Float 0-1. Low confidence sections can be flagged in results. |
| `label_source` | "pattern" or "gpt-4o-mini". Tracks how the label was assigned. |

### 1.2 Supabase `documents` Table

| Column | What It Gives You |
|---|---|
| `id` | UUID. FK target for sections. |
| `case_id` | **FK to cases table. The primary scoping filter — every search is scoped to a case.** |
| `file_name` | Document stem. Displayed in search results for provenance. |
| `document_type` | Legal classification (e.g., "Contract - NDA"). Used as a metadata filter. |
| `confidence_score` | Classification confidence. Low-confidence docs can be flagged. |
| `total_pages` | Page count. Useful for result context. |

### 1.3 Supabase `extractions` Table

| Column | What It Gives You |
|---|---|
| `id` | UUID primary key |
| `section_id` | FK to sections. Links an extraction back to its source chunk. |
| `document_id` | FK to documents. |
| `extraction_type` | "party", "date", "amount", "obligation", "claim", "condition", "evidence_ref", "case_citation" |
| `entity_name` | The extracted value (e.g., "Acme Corp", "breach of fiduciary duty") |
| `entity_value` | Normalized value (ISO date, numeric amount, etc.) |
| `properties` | JSONB with type-specific fields (plaintiff, defendant, trigger_event, etc.) |
| `confidence` | Float 0-1 |
| `page_range` | Provenance |

**How extractions help search:** Not embedded separately. Instead, extraction data is used to build filterable metadata on each section's vector row, and to power structured queries that complement semantic search (e.g., "find all sections mentioning party X" is a SQL query on extractions, not a vector search).

### 1.4 Supabase `kg_nodes` + `kg_edges` Tables

Knowledge graph data is **not embedded** in the vector store. It serves a different purpose: once the vector search returns relevant sections, agents (Phase 4) can traverse the KG to find related entities, trace claim-to-evidence paths, or detect conflicts. The KG is a query-time reasoning layer, not a retrieval layer.

### 1.5 What You Do NOT Have Yet
- Vector embeddings for sections
- A `section_embeddings` table with pgvector column
- pgvector extension enabled in Supabase
- pg_trgm extension enabled for keyword search
- A hybrid search SQL function
- An API endpoint / Python search function


---

## 2. Architecture Decisions (Locked In)

| Decision | Choice | Rationale |
|---|---|---|
| **Embedding model** | OpenAI `text-embedding-3-small` | 1536 dimensions, 8191 token context, $0.02/1M tokens. Already have the API key. Quality is strong on legal text. Swap-friendly — re-embed later if needed. |
| **Vector store** | Supabase pgvector | Single database for everything. Sections, embeddings, extractions, KG all queryable in one SQL statement. No sync issues. Performance is fine for per-case search volumes (tens to hundreds of sections per case, not millions). |
| **What gets embedded** | Sections only (enriched with metadata) | Sections are the natural chunks. Extractions and KG data become metadata filters and structured query targets, not separate embeddings. Keeps the vector index clean and fast. |
| **Search type** | Hybrid: semantic (pgvector) + keyword (pg_trgm) + structural (AST filters) | Semantic catches meaning ("payment deadline" ↔ "due date for fees"). Keyword catches exact terms lawyers search for ("Section 4.2", "Force Majeure"). Structural narrows by document type, semantic label, hierarchy level. |
| **Search scope** | Always scoped to `case_id` | Every query filters by case first. Cross-case search is intentionally blocked for data isolation and governance. |
| **Consumers** | Both agents (Phase 4) AND user-facing search endpoint | The search function is a shared utility. Agents call it programmatically with structured filters. The frontend calls it via an API endpoint with a query string. Same underlying function, different wrappers. |


---

## 3. New Supabase Schema

### 3.1 Enable Extensions

Run these once in the Supabase SQL editor:

```sql
-- Vector similarity search
CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram-based keyword/fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

### 3.2 New Table: `section_embeddings`

```sql
CREATE TABLE section_embeddings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id      UUID NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    case_id         UUID NOT NULL,  -- denormalized from documents for fast filtering

    -- The vector
    embedding       vector(1536) NOT NULL,  -- OpenAI text-embedding-3-small dimensions

    -- Denormalized metadata for filtering (avoids JOINs during search)
    document_type   TEXT,           -- e.g., "Contract - NDA"
    semantic_label  TEXT,           -- e.g., "obligation.payment"
    level           INTEGER,        -- hierarchy depth
    is_synthetic    BOOLEAN,
    page_range      TEXT,
    section_title   TEXT,           -- for display in results

    -- Keyword search target
    search_text     TEXT,           -- section_title + section_text, used for pg_trgm

    -- Bookkeeping
    embedding_model TEXT DEFAULT 'text-embedding-3-small',
    created_at      TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_section_embedding UNIQUE (section_id)
);
```

**Why denormalize?** During a vector search, Postgres needs to filter by case_id, document_type, semantic_label *and* sort by vector distance — all in one query. If these fields require JOINs to `documents` and `sections`, performance drops significantly because the planner can't push filters into the vector index scan. Denormalizing these fields onto the embeddings row means the entire search is a single-table scan with filters.

### 3.3 Indexes

```sql
-- HNSW index for approximate nearest neighbor search, scoped to case
-- HNSW is preferred over IVFFlat: no training step, better recall, slightly more memory
CREATE INDEX idx_embeddings_hnsw ON section_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- B-tree indexes for metadata filters (these get combined with the vector scan)
CREATE INDEX idx_embeddings_case_id ON section_embeddings (case_id);
CREATE INDEX idx_embeddings_doc_type ON section_embeddings (document_type);
CREATE INDEX idx_embeddings_label ON section_embeddings (semantic_label);

-- GIN trigram index for keyword search
CREATE INDEX idx_embeddings_search_text_trgm ON section_embeddings
    USING gin (search_text gin_trgm_ops);
```

### 3.4 Hybrid Search Function

This is the core search function that both agents and the frontend call. It combines three signals:

1. **Semantic similarity** — cosine distance between query embedding and stored embeddings
2. **Keyword relevance** — pg_trgm similarity between query text and search_text
3. **Structural filters** — WHERE clauses on document_type, semantic_label, level

```sql
CREATE OR REPLACE FUNCTION hybrid_search(
    query_embedding     vector(1536),
    query_text          TEXT,
    p_case_id           UUID,
    p_document_types    TEXT[]      DEFAULT NULL,   -- filter to specific doc types
    p_semantic_labels   TEXT[]      DEFAULT NULL,   -- filter to specific labels
    p_document_ids      UUID[]     DEFAULT NULL,   -- filter to specific documents
    p_min_level         INTEGER    DEFAULT NULL,    -- minimum hierarchy depth
    p_max_level         INTEGER    DEFAULT NULL,    -- maximum hierarchy depth
    p_limit             INTEGER    DEFAULT 10,
    p_semantic_weight   FLOAT      DEFAULT 0.7,    -- how much weight for vector vs keyword
    p_similarity_threshold FLOAT   DEFAULT 0.3     -- minimum cosine similarity to include
)
RETURNS TABLE (
    section_id          UUID,
    document_id         UUID,
    section_title       TEXT,
    document_type       TEXT,
    semantic_label      TEXT,
    level               INTEGER,
    page_range          TEXT,
    is_synthetic        BOOLEAN,
    semantic_score      FLOAT,
    keyword_score       FLOAT,
    combined_score      FLOAT
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH semantic_results AS (
        SELECT
            se.section_id,
            se.document_id,
            se.section_title,
            se.document_type,
            se.semantic_label,
            se.level,
            se.page_range,
            se.is_synthetic,
            1 - (se.embedding <=> query_embedding) AS sem_score
        FROM section_embeddings se
        WHERE se.case_id = p_case_id
          AND (p_document_types IS NULL OR se.document_type = ANY(p_document_types))
          AND (p_semantic_labels IS NULL OR se.semantic_label = ANY(p_semantic_labels))
          AND (p_document_ids IS NULL OR se.document_id = ANY(p_document_ids))
          AND (p_min_level IS NULL OR se.level >= p_min_level)
          AND (p_max_level IS NULL OR se.level <= p_max_level)
          AND 1 - (se.embedding <=> query_embedding) >= p_similarity_threshold
        ORDER BY se.embedding <=> query_embedding
        LIMIT p_limit * 3   -- overfetch for re-ranking
    ),
    keyword_results AS (
        SELECT
            se.section_id,
            similarity(se.search_text, query_text) AS kw_score
        FROM section_embeddings se
        WHERE se.case_id = p_case_id
          AND (p_document_types IS NULL OR se.document_type = ANY(p_document_types))
          AND (p_semantic_labels IS NULL OR se.semantic_label = ANY(p_semantic_labels))
          AND (p_document_ids IS NULL OR se.document_id = ANY(p_document_ids))
          AND (p_min_level IS NULL OR se.level >= p_min_level)
          AND (p_max_level IS NULL OR se.level <= p_max_level)
          AND se.search_text % query_text  -- trigram threshold filter
        LIMIT p_limit * 3
    ),
    combined AS (
        SELECT
            sr.section_id,
            sr.document_id,
            sr.section_title,
            sr.document_type,
            sr.semantic_label,
            sr.level,
            sr.page_range,
            sr.is_synthetic,
            sr.sem_score,
            COALESCE(kr.kw_score, 0.0) AS kw_score,
            (p_semantic_weight * sr.sem_score + (1 - p_semantic_weight) * COALESCE(kr.kw_score, 0.0)) AS combined
        FROM semantic_results sr
        LEFT JOIN keyword_results kr ON sr.section_id = kr.section_id
    )
    SELECT
        c.section_id,
        c.document_id,
        c.section_title,
        c.document_type,
        c.semantic_label,
        c.level,
        c.page_range,
        c.is_synthetic,
        c.sem_score::FLOAT AS semantic_score,
        c.kw_score::FLOAT AS keyword_score,
        c.combined::FLOAT AS combined_score
    FROM combined c
    ORDER BY c.combined DESC
    LIMIT p_limit;
END;
$$;
```


---

## 4. The Embedding Text Strategy

What text do you actually embed? Not just raw `section_text` — you prepend structural context so the embedding captures *where* this section sits in the document.

### 4.1 Embedding Input Construction

For each section, build the embedding input as:

```
[{document_type}] [{semantic_label}] {section_title}

{section_text}
```

**Example:**

```
[Contract - NDA] [obligation.payment] Section 4.2: Payment Terms

The Receiving Party shall pay the Disclosing Party a licensing fee of
$50,000 within 30 days of the Effective Date. Late payments shall accrue
interest at a rate of 1.5% per month...
```

**Why this format?** The embedding model encodes the *meaning* of the entire input. By prepending document type and semantic label, sections with the same label across different documents will cluster together in vector space. A search for "payment obligations" will naturally rank `[obligation.payment]` sections higher, even if the text itself uses different wording.

### 4.2 The `search_text` Column (For Keyword Search)

This is simpler — just `section_title + "\n" + section_text`. No metadata prefixes, because keyword search is looking for exact term matches, and you don't want a search for "contract" to match every section that has `[Contract - NDA]` in its embedding prefix.

### 4.3 Handling Long Sections

OpenAI `text-embedding-3-small` accepts up to 8191 tokens (~32,000 chars). Most sections from Phase 1 are well under this. However, if `00_section_refine.py` from Phase 2 didn't catch a section, or if a section is still very long:

- If `len(embedding_input) > 28000` chars (~7000 tokens, leaving buffer): truncate to 28000 chars with a warning log.
- This should be rare. The section refiner in Phase 2 already splits sections > 4000 chars.
- Log any truncations so you can review whether the refiner needs adjustment.


---

## 5. Script Architecture

All scripts live in `backend/03_SEARCH/`. They follow the same conventions as 01_INITIAL and 02_MIDDLE: numbered scripts, CLI args, `SUCCESS:`/`ERROR:` output, intermediate files in `zz_temp_chunks/`.

### 5.1 Script: `01_embed_sections.py`

**Purpose:** Read sections from Supabase for a given case_id (via document_id → case_id join), generate OpenAI embeddings, and upsert into `section_embeddings`.

**Input:** `case_id` (CLI arg). Optionally `--document_id` to embed a single document.

**Output:** Rows upserted into `section_embeddings`. Summary written to `zz_temp_chunks/{case_id}_embedding_summary.json`.

**Process:**

1. Query Supabase for all sections belonging to documents in this case. Join to `documents` to get `case_id`, `document_type`. Join to `sections` for all fields.
2. Filter out sections that already have embeddings (check `section_embeddings.section_id`). This makes re-runs safe — only new/updated sections get embedded.
3. For each section, build the embedding input string (see §4.1).
4. Batch sections into groups of 100 (OpenAI embeddings API supports batch input).
5. Call `openai.embeddings.create(model="text-embedding-3-small", input=batch)`.
6. Upsert each result into `section_embeddings` with all denormalized metadata.
7. Write summary: `{total_sections, newly_embedded, skipped_existing, truncated, errors}`.

**Key details:**
- Uses `ON CONFLICT (section_id) DO UPDATE` so re-embedding after Phase 2 changes is safe.
- Rate limiting: OpenAI embedding API is generous (3500 RPM for small), but add a 0.1s sleep between batches as a courtesy.
- Cost estimate: 100 sections × ~500 tokens avg = 50,000 tokens = $0.001. Essentially free.

**Estimated effort:** 1 day. ~150 lines.

### 5.2 Script: `02_search.py`

**Purpose:** The core search module. Exposes a `hybrid_search()` Python function that wraps the SQL function from §3.4. This is the function that both agents and the API endpoint call.

**Input:** Query string + optional filters (case_id, document_types, semantic_labels, document_ids, level range).

**Output:** List of ranked result dicts with section metadata, scores, and full section text.

**Process:**

1. Embed the query string using `openai.embeddings.create()`.
2. Call the `hybrid_search` SQL function via Supabase RPC, passing the query embedding, raw query text, case_id, and all filters.
3. For each returned section_id, fetch full `section_text` from the `sections` table (not stored in embeddings table to keep it lean).
4. Optionally fetch parent section context (title + first 200 chars of parent's text) for result display.
5. Return structured results:

```python
{
    "query": "payment deadline",
    "case_id": "uuid-...",
    "filters_applied": {"document_types": ["Contract - NDA"], "semantic_labels": null},
    "results": [
        {
            "section_id": "uuid-...",
            "document_id": "uuid-...",
            "file_name": "Acme_NDA_2024",
            "document_type": "Contract - NDA",
            "section_title": "Section 4.2: Payment Terms",
            "semantic_label": "obligation.payment",
            "level": 2,
            "page_range": "5-6",
            "parent_context": "Article IV: Financial Terms",
            "section_text": "The Receiving Party shall pay...",
            "scores": {
                "semantic": 0.87,
                "keyword": 0.42,
                "combined": 0.74
            },
            "provenance": {
                "anchor_id": "ai-chunk-00042",
                "is_synthetic": false,
                "start_page": 5,
                "end_page": 6
            }
        }
    ],
    "total_results": 8
}
```

**This script is importable, not just CLI.** Agents in Phase 4 will `from backend.O3_SEARCH.O2_search import hybrid_search`. The CLI wrapper is for testing:

```bash
python 02_search.py --case_id "uuid-..." --query "payment deadline" --limit 5
python 02_search.py --case_id "uuid-..." --query "breach of fiduciary duty" --doc_types "Pleading - Complaint"
python 02_search.py --case_id "uuid-..." --query "Exhibit A" --labels "exhibit_reference,exhibit_content"
```

**Estimated effort:** 1 day. ~200 lines.

### 5.3 Script: `03_search_api.py` (Optional — for frontend)

**Purpose:** A lightweight FastAPI (or Flask) endpoint that wraps `02_search.py` for the frontend to call.

**Endpoint:**

```
POST /api/search
{
    "case_id": "uuid-...",
    "query": "payment deadline",
    "filters": {
        "document_types": ["Contract - NDA"],
        "semantic_labels": ["obligation.payment", "obligation.delivery"],
        "document_ids": null,
        "min_level": null,
        "max_level": null
    },
    "limit": 10,
    "semantic_weight": 0.7
}
```

**Response:** Same structure as §5.2 output.

**Key details:**
- Validates `case_id` against the user's session/permissions (the frontend auth layer handles this).
- Caches the OpenAI embedding for identical query strings within a short window (avoid re-embedding the same query if a user paginates or adjusts filters).
- Returns provenance links (page_range, anchor_id, file_name) so the frontend can deep-link to the source.

**Estimated effort:** Half a day. ~80 lines. Can defer until frontend work begins.

### 5.4 `main.py` Update

Add step 01 (embedding) to the orchestrator. Search (02) and API (03) are query-time — they don't run in the pipeline.

```
Orchestrator chain:
  01_INITIAL (steps 01-08) → 02_MIDDLE (steps 09-12+) → 03_SEARCH (step 01: embed)
```

After embedding completes, the case is "search-ready." The search function is always available for queries.


---

## 6. Search Patterns: How Lawyers Will Actually Search

These patterns inform how you tune weights, filters, and result presentation.

### 6.1 Broad Case Exploration
**Query:** "What does this case involve?"
**Filters:** case_id only, no label/type filters.
**Expected behavior:** Returns top sections across all documents — introduction, statement of facts, nature of action. Semantic search handles this naturally because these sections contain overview language.

### 6.2 Targeted Clause Finding
**Query:** "payment terms" or "indemnification"
**Filters:** case_id + `document_type = 'Contract%'` + `semantic_label IN ('obligation.payment', 'indemnification.scope')`
**Expected behavior:** Narrow results to contract sections with matching labels. Both semantic and keyword signals fire here.

### 6.3 Cross-Document Evidence Tracing
**Query:** "Exhibit A" or "breach occurred on March 15"
**Filters:** case_id only (search across all doc types).
**Expected behavior:** Finds references to the exhibit in complaints, motions, and the exhibit content itself. Keyword search is critical here — "Exhibit A" is an exact term, not a semantic concept.

### 6.4 Legal Standard Lookup
**Query:** "likelihood of success on the merits"
**Filters:** case_id + `semantic_label LIKE 'argument%' OR semantic_label LIKE 'legal_standard%'`
**Expected behavior:** Finds the legal argument sections discussing this standard. Semantic search dominates — the exact phrasing may vary across documents.

### 6.5 Party-Specific Search
**Query:** "Acme Corporation obligations"
**Filters:** case_id + `document_type = 'Contract%'`
**Note:** This is where extractions help *after* retrieval. The vector search finds obligation-related sections, then the agent checks the `extractions` table for rows where `entity_name = 'Acme Corporation'` and `extraction_type = 'obligation'` to confirm relevance.


---

## 7. Data Governance Considerations

### 7.1 Embedding API and Sensitive Data

OpenAI's embedding API (`text-embedding-3-small`) sends section text to OpenAI's servers. Per your governance research:

- **OpenAI's data policy for API:** As of 2024, OpenAI does **not** use API inputs/outputs for training. This is documented in their API data usage policy. Verify this hasn't changed before production deployment.
- **What gets sent:** Only the section text + structural prefix. No case_id, no client names (unless they appear in the document text itself).
- **Mitigation options if needed:**
  - Use the `no-store` header if OpenAI supports it for embeddings.
  - If a client requires fully local processing: swap to a local embedding model (e.g., `sentence-transformers/all-MiniLM-L6-v2` via HuggingFace). The architecture supports this — just change the embedding call in `01_embed_sections.py` and update the vector dimension from 1536 to 384.
  - For the highest sensitivity cases, consider anonymizing section text before embedding (replace party names with PARTY_A, PARTY_B using the extractions table).

### 7.2 Search Result Provenance

Every search result carries provenance metadata (section_id, page_range, anchor_id, document file_name). This supports the HITL principle — lawyers can always trace a search result back to the exact source location in the original document.

### 7.3 Case Isolation

The `case_id` filter is mandatory in `hybrid_search`. There is no code path that searches across cases. This is enforced at the SQL function level — `p_case_id` is a required parameter with no default.


---

## 8. Re-embedding Strategy

When does a section need re-embedding?

| Trigger | Action |
|---|---|
| New document processed through pipeline | `01_embed_sections.py` picks up new sections automatically (UPSERT) |
| Section text updated (e.g., section refiner re-runs) | Re-embed that section. The UPSERT on `section_id` replaces the old embedding. |
| Semantic label changes (e.g., HITL correction) | Re-embed. The label is part of the embedding input prefix. |
| Document deleted | CASCADE delete on `section_embeddings` via FK handles this. |
| Embedding model upgrade | Full re-embed for the case. Add a `--force` flag to `01_embed_sections.py` that ignores existing embeddings. |

**The key insight:** Because the embedding input includes `[document_type] [semantic_label]`, any change to those fields means the embedding is stale and should be regenerated. The script should detect this by comparing stored `document_type`/`semantic_label` on the embedding row against the current values on the sections/documents tables.


---

## 9. Dependencies

### 9.1 New Python Libraries

| Library | Usage |
|---|---|
| `openai` | Already installed. Used for `openai.embeddings.create()`. |
| `numpy` | For any local vector operations if needed (distance calculations, normalization). Likely already installed as a pandas dependency. |
| `fastapi` + `uvicorn` | For the search API endpoint (script 03). Optional — defer until frontend work. |

### 9.2 Environment Variables

No new env vars needed. Uses existing:
- `OPENAI_API_KEY` — for embeddings
- `SUPABASE_URL` — for database access
- `SUPABASE_SERVICE_ROLE_KEY` — for database access

### 9.3 Supabase Setup (One-Time)

- Enable `vector` extension
- Enable `pg_trgm` extension
- Run the `CREATE TABLE section_embeddings` statement
- Run the `CREATE INDEX` statements
- Run the `CREATE FUNCTION hybrid_search` statement


---

## 10. Estimated Effort & Sequence

| Step | Script | Effort | Depends On |
|---|---|---|---|
| 1. Supabase schema setup | SQL statements (§3) | 30 minutes | pgvector extension enabled |
| 2. Embedding script | `01_embed_sections.py` | 1 day | Schema setup complete |
| 3. Search module | `02_search.py` | 1 day | Embedding script complete (need test data) |
| 4. Search API | `03_search_api.py` | Half day | Search module complete. Can defer. |
| 5. Testing & tuning | Weight tuning, threshold adjustment | Half day | Search module complete |

**Total: ~3 days of implementation.**

### 10.1 Directory Structure

```
backend/03_SEARCH/
├── 01_embed_sections.py      # Embedding pipeline script
├── 02_search.py              # Core search module (importable + CLI)
├── 03_search_api.py          # FastAPI endpoint wrapper (optional, defer)
├── main.py                   # Orchestrator for embedding step
└── README.md                 # Quick-start guide
```


---

## 11. Testing Plan

### 11.1 Embedding Verification

After running `01_embed_sections.py` on a test case:

```sql
-- Verify all sections have embeddings
SELECT d.file_name, COUNT(s.id) AS total_sections, COUNT(se.id) AS embedded
FROM documents d
JOIN sections s ON s.document_id = d.id
LEFT JOIN section_embeddings se ON se.section_id = s.id
WHERE d.case_id = '<test_case_id>'
GROUP BY d.file_name;
```

### 11.2 Search Quality Smoke Tests

Run these queries against a test case and manually verify the top 3 results make sense:

1. `"payment terms"` → should return obligation.payment sections from contracts
2. `"breach of contract"` → should return causes_of_action sections from complaints
3. `"Exhibit A"` → should return exhibit references across documents (keyword-heavy)
4. `"what happened on [specific date]"` → should return factual_allegations and statement_of_facts sections
5. `"governing law"` → should return dispute_resolution sections

### 11.3 Edge Cases to Watch

- **Empty sections:** Sections with no text (title-only rows, TOC entries). Skip these during embedding — they have no semantic content.
- **Very short sections:** Signature blocks, certificate of service, jury demand. These embed fine but rarely match meaningful queries. Consider lowering their result ranking or excluding them from search by default.
- **Duplicate content:** If the same exhibit is referenced in multiple documents, the search should return all references (they're different sections from different documents, even if the text is similar). This is correct behavior — the lawyer needs to see where the exhibit is cited.