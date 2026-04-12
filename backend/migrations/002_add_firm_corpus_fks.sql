-- Migration 002: Schema alterations
-- Adds firm_id to cases, converts documents.case_id and kg_nodes.case_id
-- from TEXT to UUID, adds corpus_id FK to documents, adds firm_id to
-- agent_responses and document_processing_steps.
--
-- PRE-REQUISITE: Migration 001 must have run (firms + corpus tables exist).
-- WARNING: Run during maintenance window with pipeline paused.
--          The TEXT->UUID conversion is destructive if values aren't valid UUIDs.
--          Pre-flight check: all existing values confirmed valid as of 2026-04-10.

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- 1. Add firm_id to cases
-- ══════════════════════════════════════════════════════════════

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id);

-- Backfill all existing cases to Default Firm
UPDATE cases
SET firm_id = '00000000-0000-4000-a000-000000000001'
WHERE firm_id IS NULL;

ALTER TABLE cases
    ALTER COLUMN firm_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cases_firm_id ON cases (firm_id);

-- ══════════════════════════════════════════════════════════════
-- 2. Convert documents.case_id from TEXT to UUID
-- ══════════════════════════════════════════════════════════════

-- Step 2a: create temp UUID column
ALTER TABLE documents ADD COLUMN case_id_uuid UUID;

-- Step 2b: cast existing values (all confirmed valid UUIDs)
UPDATE documents
SET case_id_uuid = case_id::uuid
WHERE case_id IS NOT NULL AND case_id != '';

-- Step 2c: drop old TEXT column
ALTER TABLE documents DROP COLUMN case_id;

-- Step 2d: rename new column
ALTER TABLE documents RENAME COLUMN case_id_uuid TO case_id;

-- Step 2e: add FK constraint
ALTER TABLE documents
    ADD CONSTRAINT documents_case_id_fkey
    FOREIGN KEY (case_id) REFERENCES cases(id);

CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents (case_id);

-- ══════════════════════════════════════════════════════════════
-- 3. Convert kg_nodes.case_id from TEXT to UUID
-- ══════════════════════════════════════════════════════════════

ALTER TABLE kg_nodes ADD COLUMN case_id_uuid UUID;

UPDATE kg_nodes
SET case_id_uuid = case_id::uuid
WHERE case_id IS NOT NULL AND case_id != '';

ALTER TABLE kg_nodes DROP COLUMN case_id;
ALTER TABLE kg_nodes RENAME COLUMN case_id_uuid TO case_id;

-- ══════════════════════════════════════════════════════════════
-- 4. Add corpus_id FK to documents (nullable for migration period)
-- ══════════════════════════════════════════════════════════════

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS corpus_id UUID REFERENCES corpus(id);

CREATE INDEX IF NOT EXISTS idx_documents_corpus_id ON documents (corpus_id);

-- ══════════════════════════════════════════════════════════════
-- 5. Add firm_id to tables that need direct firm scoping
-- ══════════════════════════════════════════════════════════════

-- agent_responses already has case_id UUID — add firm_id for direct RLS
ALTER TABLE agent_responses
    ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id);

-- Backfill from cases
UPDATE agent_responses ar
SET firm_id = (SELECT c.firm_id FROM cases c WHERE c.id = ar.case_id)
WHERE ar.firm_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_responses_firm_id ON agent_responses (firm_id);

-- document_processing_steps already has case_id UUID — add firm_id
ALTER TABLE document_processing_steps
    ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id);

UPDATE document_processing_steps dps
SET firm_id = (SELECT c.firm_id FROM cases c WHERE c.id = dps.case_id)
WHERE dps.firm_id IS NULL AND dps.case_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dps_firm_id ON document_processing_steps (firm_id);

COMMIT;
