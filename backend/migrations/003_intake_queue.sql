-- Migration 003: Intake queue + notifications tables
-- PRE-REQUISITE: Migration 001 (firms table exists).

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- 1. Enums for intake queue
-- ══════════════════════════════════════════════════════════════

DO $$ BEGIN
    CREATE TYPE intake_status AS ENUM (
        'pending',                -- just received, not yet routed
        'routing',                -- routing worker is analyzing
        'awaiting_confirmation',  -- low-confidence route, needs user decision
        'confirmed',              -- route decided (auto or user), waiting for scheduler
        'scheduled',              -- scheduler has assigned a processing window
        'processing',             -- pipeline is running
        'completed',              -- pipeline finished successfully
        'failed',                 -- pipeline or routing error
        'cancelled'               -- user cancelled
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE intake_priority AS ENUM (
        'immediate',  -- process now (hearing in 2 hours)
        'soon',       -- drain every 15-30 min (CMS webhooks, moderate urgency)
        'overnight',  -- drain at 1am in bulk mode (default for email, drives)
        'manual'      -- wait for explicit trigger (firm onboarding bulk imports)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE processing_mode AS ENUM (
        'accuracy',   -- slower, better models, multi-pass (default for overnight)
        'balanced',   -- current behavior (default for soon)
        'fast'        -- cheap models, single-pass (default for immediate)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ══════════════════════════════════════════════════════════════
-- 2. intake_queue table
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS intake_queue (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id          UUID NOT NULL REFERENCES firms(id),

    -- Source tracking
    source_channel   TEXT NOT NULL,             -- 'upload' | 'email' | 'gdrive' | 'dropbox' | 'cms_webhook'
    source_ref       TEXT,                      -- external ID (email message-id, drive file id, etc.)
    source_metadata  JSONB NOT NULL DEFAULT '{}', -- channel-specific data (sender, subject, folder, etc.)

    -- File info
    file_path        TEXT,                      -- path in Supabase storage or local fs
    file_name        TEXT,                      -- original filename
    file_hash        TEXT,                      -- SHA-256 for deduplication

    -- Status + scheduling
    status           intake_status NOT NULL DEFAULT 'pending',
    process_priority intake_priority NOT NULL DEFAULT 'soon',
    scheduled_for    TIMESTAMPTZ,               -- NULL = process at next opportunity for priority
    processing_mode  processing_mode NOT NULL DEFAULT 'balanced',

    -- Routing
    routing_result   JSONB,                     -- {suggested_case_id, suggested_corpus_id, confidence, method, reasoning, candidates[]}
    user_decision    JSONB,                     -- {action: 'confirm'|'reassign'|'new_case'|'reject', decided_by, decided_at}
    explicit_case_hint TEXT,                    -- from plus-address, API header, or UI pre-fill

    -- Target (set after routing resolves)
    target_case_id   UUID REFERENCES cases(id),
    target_corpus_id UUID REFERENCES corpus(id),

    -- Error tracking
    error_message    TEXT,
    retry_count      INTEGER NOT NULL DEFAULT 0,

    -- Timestamps
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ
);

-- Active queue items (exclude completed/cancelled for scheduler queries)
CREATE INDEX IF NOT EXISTS idx_intake_queue_active
    ON intake_queue (firm_id, status, created_at)
    WHERE status NOT IN ('completed', 'cancelled');

-- Scheduler: find items ready to process by priority
CREATE INDEX IF NOT EXISTS idx_intake_queue_schedulable
    ON intake_queue (process_priority, created_at)
    WHERE status IN ('confirmed', 'scheduled');

-- Overnight batch: find items scheduled for a specific window
CREATE INDEX IF NOT EXISTS idx_intake_queue_overnight
    ON intake_queue (scheduled_for)
    WHERE status IN ('confirmed', 'scheduled')
      AND process_priority = 'overnight';

-- Deduplication: find items with same file hash
CREATE INDEX IF NOT EXISTS idx_intake_queue_file_hash
    ON intake_queue (file_hash)
    WHERE file_hash IS NOT NULL;

-- ══════════════════════════════════════════════════════════════
-- 3. notifications table
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id     UUID NOT NULL REFERENCES firms(id),
    event_type  TEXT NOT NULL,                  -- 'intake_needs_routing' | 'intake_completed' | 'intake_failed' |
                                                -- 'pipeline_completed' | 'pipeline_error' |
                                                -- 'review_needed' | 'morning_summary' |
                                                -- 'corpus_updated' | 'batch_completed'
    payload     JSONB NOT NULL DEFAULT '{}',    -- event-specific data
    read        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unread notifications per firm (most common query)
CREATE INDEX IF NOT EXISTS idx_notifications_firm_unread
    ON notifications (firm_id, created_at DESC)
    WHERE read = false;

-- ══════════════════════════════════════════════════════════════
-- 4. Enable Supabase Realtime on notifications + intake_queue
-- ══════════════════════════════════════════════════════════════

ALTER PUBLICATION supabase_realtime ADD TABLE notifications;
ALTER PUBLICATION supabase_realtime ADD TABLE intake_queue;

COMMIT;
