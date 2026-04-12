-- Migration 005: RLS policies + public.get_firm_id() helper
--
-- CRITICAL: This is the highest-risk migration in the entire project.
-- A bug here leaks privileged client data across firms.
--
-- MUST be tested with backend/tests/test_rls_isolation.sql BEFORE any
-- real multi-firm data lands.
--
-- Design principles:
--   1. No policy stacking — each child table re-derives firm access from scratch
--   2. Default deny on NULL — if corpus_id IS NULL and case_id IS NULL, row is invisible
--   3. Shared corpus = read-only — prevents Firm B writing into Firm A's shared corpus
--   4. Service role key bypasses all RLS — backend workers are unaffected
--
-- PRE-REQUISITE: Migrations 001, 002, 003 must have run.

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- 0. Helper function: extract firm_id from JWT
-- ══════════════════════════════════════════════════════════════

-- NOTE: Supabase hosted restricts auth schema — use public schema instead
CREATE OR REPLACE FUNCTION public.get_firm_id() RETURNS UUID AS $$
    -- Returns the firm_id claim from the JWT.
    -- If JWT is absent or claim is missing, returns a sentinel UUID
    -- that will NEVER match a real firm_id → default deny.
    SELECT COALESCE(
        (current_setting('request.jwt.claims', true)::json ->> 'firm_id')::uuid,
        '00000000-0000-0000-0000-000000000000'::uuid
    );
$$ LANGUAGE sql STABLE SECURITY DEFINER;

COMMENT ON FUNCTION public.get_firm_id() IS
    'Extracts firm_id from JWT claims. Returns sentinel UUID on missing/malformed JWT → zero rows.';

-- ══════════════════════════════════════════════════════════════
-- 1. FIRMS — user sees only their own firm
-- ══════════════════════════════════════════════════════════════

ALTER TABLE firms ENABLE ROW LEVEL SECURITY;
ALTER TABLE firms FORCE ROW LEVEL SECURITY;

CREATE POLICY firms_select ON firms
    FOR SELECT
    USING (id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 2. CORPUS — own firm OR shared
-- ══════════════════════════════════════════════════════════════

ALTER TABLE corpus ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus FORCE ROW LEVEL SECURITY;

-- Read: own firm's corpora + any shared corpora (requires valid auth)
CREATE POLICY corpus_select ON corpus
    FOR SELECT
    USING (
        firm_id = public.get_firm_id()
        OR (is_shared_across_firms = true
            AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
    );

-- Write: only own firm's corpora (never shared ones from other firms)
CREATE POLICY corpus_insert ON corpus
    FOR INSERT
    WITH CHECK (firm_id = public.get_firm_id());

CREATE POLICY corpus_update ON corpus
    FOR UPDATE
    USING (firm_id = public.get_firm_id())
    WITH CHECK (firm_id = public.get_firm_id());

CREATE POLICY corpus_delete ON corpus
    FOR DELETE
    USING (firm_id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 3. CASES — strictly firm-scoped
-- ══════════════════════════════════════════════════════════════

ALTER TABLE cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases FORCE ROW LEVEL SECURITY;

CREATE POLICY cases_select ON cases
    FOR SELECT USING (firm_id = public.get_firm_id());

CREATE POLICY cases_insert ON cases
    FOR INSERT WITH CHECK (firm_id = public.get_firm_id());

CREATE POLICY cases_update ON cases
    FOR UPDATE
    USING (firm_id = public.get_firm_id())
    WITH CHECK (firm_id = public.get_firm_id());

CREATE POLICY cases_delete ON cases
    FOR DELETE USING (firm_id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 4. DOCUMENTS — highest-risk table, 3 SELECT paths
--
--    Path A: corpus owned by my firm
--    Path B: shared corpus (SELECT only)
--    Path C: no corpus, fall back to case ownership (migration period)
--
--    INSERT/UPDATE: only own firm's corpus or own firm's case (no shared)
-- ══════════════════════════════════════════════════════════════

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_select ON documents
    FOR SELECT
    USING (
        -- Path A: document belongs to a corpus owned by my firm
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.firm_id = public.get_firm_id()
        )
        OR
        -- Path B: document belongs to a shared corpus (read-only access)
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.is_shared_across_firms = true
        )
        OR
        -- Path C: no corpus assigned, fall back to case ownership
        -- (for documents not yet backfilled to a corpus)
        (
            documents.corpus_id IS NULL
            AND documents.case_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM cases ca
                WHERE ca.id = documents.case_id
                  AND ca.firm_id = public.get_firm_id()
            )
        )
    );

-- INSERT: only into own firm's corpus or own firm's case
-- IMPORTANT: no Path B here — cannot insert into shared corpora
CREATE POLICY documents_insert ON documents
    FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.firm_id = public.get_firm_id()
        )
        OR (
            documents.corpus_id IS NULL
            AND documents.case_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM cases ca
                WHERE ca.id = documents.case_id
                  AND ca.firm_id = public.get_firm_id()
            )
        )
    );

CREATE POLICY documents_update ON documents
    FOR UPDATE
    USING (
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.firm_id = public.get_firm_id()
        )
        OR (
            documents.corpus_id IS NULL
            AND documents.case_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM cases ca
                WHERE ca.id = documents.case_id
                  AND ca.firm_id = public.get_firm_id()
            )
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.firm_id = public.get_firm_id()
        )
        OR (
            documents.corpus_id IS NULL
            AND documents.case_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM cases ca
                WHERE ca.id = documents.case_id
                  AND ca.firm_id = public.get_firm_id()
            )
        )
    );

CREATE POLICY documents_delete ON documents
    FOR DELETE
    USING (
        EXISTS (
            SELECT 1 FROM corpus c
            WHERE c.id = documents.corpus_id
              AND c.firm_id = public.get_firm_id()
        )
        OR (
            documents.corpus_id IS NULL
            AND documents.case_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM cases ca
                WHERE ca.id = documents.case_id
                  AND ca.firm_id = public.get_firm_id()
            )
        )
    );

-- ══════════════════════════════════════════════════════════════
-- 5. SECTIONS — follows document access via corpus chain
-- ══════════════════════════════════════════════════════════════

ALTER TABLE sections ENABLE ROW LEVEL SECURITY;
ALTER TABLE sections FORCE ROW LEVEL SECURITY;

CREATE POLICY sections_select ON sections
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = sections.document_id
              AND (
                  EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                  OR (EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.is_shared_across_firms = true)
                      AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
                  OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                      AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
              )
        )
    );

CREATE POLICY sections_mutate ON sections
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = sections.document_id
              AND (
                  EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                  OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                      AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
              )
        )
    );

-- ══════════════════════════════════════════════════════════════
-- 6. SECTION_EMBEDDINGS — follows document access
-- ══════════════════════════════════════════════════════════════

ALTER TABLE section_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE section_embeddings FORCE ROW LEVEL SECURITY;

CREATE POLICY section_embeddings_select ON section_embeddings
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = section_embeddings.document_id
              AND (
                  EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                  OR (EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.is_shared_across_firms = true)
                      AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
                  OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                      AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
              )
        )
    );

-- ══════════════════════════════════════════════════════════════
-- 7. INTAKE_QUEUE — strictly firm-scoped
-- ══════════════════════════════════════════════════════════════

ALTER TABLE intake_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE intake_queue FORCE ROW LEVEL SECURITY;

CREATE POLICY intake_queue_select ON intake_queue
    FOR SELECT USING (firm_id = public.get_firm_id());

CREATE POLICY intake_queue_insert ON intake_queue
    FOR INSERT WITH CHECK (firm_id = public.get_firm_id());

CREATE POLICY intake_queue_update ON intake_queue
    FOR UPDATE
    USING (firm_id = public.get_firm_id())
    WITH CHECK (firm_id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 8. NOTIFICATIONS — strictly firm-scoped
-- ══════════════════════════════════════════════════════════════

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;

CREATE POLICY notifications_select ON notifications
    FOR SELECT USING (firm_id = public.get_firm_id());

CREATE POLICY notifications_update ON notifications
    FOR UPDATE
    USING (firm_id = public.get_firm_id())
    WITH CHECK (firm_id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 9. AGENT_RESPONSES — firm-scoped
-- ══════════════════════════════════════════════════════════════

ALTER TABLE agent_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_responses FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_responses_select ON agent_responses
    FOR SELECT USING (firm_id = public.get_firm_id());

-- ══════════════════════════════════════════════════════════════
-- 10. DOCUMENT_PROCESSING_STEPS — firm-scoped
-- ══════════════════════════════════════════════════════════════

ALTER TABLE document_processing_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_processing_steps FORCE ROW LEVEL SECURITY;

CREATE POLICY dps_select ON document_processing_steps
    FOR SELECT USING (firm_id = public.get_firm_id());

CREATE POLICY dps_insert ON document_processing_steps
    FOR INSERT WITH CHECK (
        firm_id = public.get_firm_id()
        OR firm_id IS NULL  -- allow NULL during pipeline runs (service role fills it later)
    );

CREATE POLICY dps_update ON document_processing_steps
    FOR UPDATE USING (firm_id = public.get_firm_id() OR firm_id IS NULL);

-- ══════════════════════════════════════════════════════════════
-- 11. REVIEWS — follows agent_response access
-- ══════════════════════════════════════════════════════════════

ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews FORCE ROW LEVEL SECURITY;

CREATE POLICY reviews_select ON reviews
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM agent_responses ar
            WHERE ar.id = reviews.agent_response_id
              AND ar.firm_id = public.get_firm_id()
        )
    );

-- ══════════════════════════════════════════════════════════════
-- 12. KG tables — follow document/case access
-- ══════════════════════════════════════════════════════════════

ALTER TABLE kg_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_nodes FORCE ROW LEVEL SECURITY;

CREATE POLICY kg_nodes_select ON kg_nodes
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = kg_nodes.document_id
              AND (
                  EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                  OR (EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.is_shared_across_firms = true)
                      AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
                  OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                      AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
              )
        )
    );

ALTER TABLE kg_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_edges FORCE ROW LEVEL SECURITY;

CREATE POLICY kg_edges_select ON kg_edges
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM kg_nodes kn
            JOIN documents d ON d.id = kn.document_id
            WHERE kn.id = kg_edges.source_id
              AND (
                  EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                  OR (EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.is_shared_across_firms = true)
                      AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
                  OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                      AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
              )
        )
    );

-- ══════════════════════════════════════════════════════════════
-- 13. Extraction tables — follow document access
-- ══════════════════════════════════════════════════════════════

-- extractions
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'extractions') THEN
        ALTER TABLE extractions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE extractions FORCE ROW LEVEL SECURITY;
        EXECUTE 'CREATE POLICY extractions_select ON extractions FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id = extractions.document_id
                  AND (
                      EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.firm_id = public.get_firm_id())
                      OR (EXISTS (SELECT 1 FROM corpus c WHERE c.id = d.corpus_id AND c.is_shared_across_firms = true)
                      AND public.get_firm_id() != '00000000-0000-0000-0000-000000000000'::uuid)
                      OR (d.corpus_id IS NULL AND d.case_id IS NOT NULL
                          AND EXISTS (SELECT 1 FROM cases ca WHERE ca.id = d.case_id AND ca.firm_id = public.get_firm_id()))
                  )
            )
        )';
    END IF;
END $$;

-- claims (has case_id directly)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'claims') THEN
        ALTER TABLE claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE claims FORCE ROW LEVEL SECURITY;
        EXECUTE 'CREATE POLICY claims_select ON claims FOR SELECT USING (
            EXISTS (SELECT 1 FROM cases ca WHERE ca.id = claims.case_id AND ca.firm_id = public.get_firm_id())
        )';
    END IF;
END $$;

-- counts (has case_id directly)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'counts') THEN
        ALTER TABLE counts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE counts FORCE ROW LEVEL SECURITY;
        EXECUTE 'CREATE POLICY counts_select ON counts FOR SELECT USING (
            EXISTS (SELECT 1 FROM cases ca WHERE ca.id = counts.case_id AND ca.firm_id = public.get_firm_id())
        )';
    END IF;
END $$;

COMMIT;
