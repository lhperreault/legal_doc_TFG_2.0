-- ============================================================
-- Add extraction_status to pipeline_jobs for the pause/resume flow
-- ============================================================
-- Run this in Supabase SQL Editor
-- ============================================================

-- Track where in Phase 2 we are
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS extraction_status TEXT DEFAULT 'not_started',
    -- 'not_started', 'awaiting_extraction', 'extraction_complete', 'kg_complete'
    ADD COLUMN IF NOT EXISTS extraction_method TEXT,
    -- 'claude' (via MCP in chat), 'gemini' (pipeline bulk), 'claude-batch' (off-hours)
    ADD COLUMN IF NOT EXISTS document_id UUID,
    -- set after Phase 1 creates the document record
    ADD COLUMN IF NOT EXISTS phase_completed INTEGER DEFAULT 0;
    -- 0 = not started, 1 = Phase 1 done, 2 = Phase 2 partial (awaiting extraction),
    -- 25 = extraction done, 27 = KG done, 3 = embeddings done, 4 = fully complete

-- Index for finding jobs awaiting Claude extraction
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_extraction_status
    ON pipeline_jobs(extraction_status) WHERE extraction_status = 'awaiting_extraction';
