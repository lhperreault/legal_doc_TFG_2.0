-- Migration 001: Foundation tables — firms + corpus model
-- Run FIRST before any other migrations in this series.

-- ══════════════════════════════════════════════════════════════
-- 1. firms table — minimal multi-tenant scaffolding
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS firms (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed a default firm so existing data can be backfilled
INSERT INTO firms (id, name)
VALUES ('00000000-0000-4000-a000-000000000001', 'Default Firm')
ON CONFLICT (id) DO NOTHING;

-- ══════════════════════════════════════════════════════════════
-- 2. corpus_type enum + corpus table
-- ══════════════════════════════════════════════════════════════

DO $$ BEGIN
    CREATE TYPE corpus_type AS ENUM (
        'active_case',   -- current live case; full agentic state
        'historical',    -- firm's old closed cases; bulk ingested; read-only retrieval
        'precedent',     -- CENDOJ rulings and similar; shared across firms (Phase 2)
        'legislation'    -- statutes and codes; shared across firms (Phase 2)
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS corpus (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   TEXT NOT NULL,
    type                   corpus_type NOT NULL DEFAULT 'active_case',
    firm_id                UUID NOT NULL REFERENCES firms(id),

    -- Jurisdictional metadata (useful for precedent/legislation corpora)
    jurisdiction           TEXT,
    court_level            TEXT,
    territorial            TEXT,
    date_start             DATE,
    date_end               DATE,

    -- Source tracking (email import, drive sync, CMS push, etc.)
    source                 TEXT,
    source_url             TEXT,

    -- Access control
    is_shared_across_firms BOOLEAN NOT NULL DEFAULT false,
    is_active_workspace    BOOLEAN NOT NULL DEFAULT true,

    -- Arbitrary metadata (e.g., import batch id, original folder path)
    metadata               JSONB NOT NULL DEFAULT '{}',

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common access patterns
CREATE INDEX IF NOT EXISTS idx_corpus_firm_id
    ON corpus (firm_id);

CREATE INDEX IF NOT EXISTS idx_corpus_type
    ON corpus (type);

CREATE INDEX IF NOT EXISTS idx_corpus_shared
    ON corpus (is_shared_across_firms)
    WHERE is_shared_across_firms = true;

CREATE INDEX IF NOT EXISTS idx_corpus_firm_active
    ON corpus (firm_id, is_active_workspace)
    WHERE is_active_workspace = true;
