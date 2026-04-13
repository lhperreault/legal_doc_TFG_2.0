-- ============================================================
-- PIPELINE JOBS: Queue table for storage-triggered processing
-- ============================================================
-- Run this in Supabase SQL Editor
-- ============================================================

-- Job queue: every file upload creates a job here
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,

    -- What to process
    bucket TEXT NOT NULL,
    folder TEXT NOT NULL,
    file_path TEXT NOT NULL,              -- full path in storage: {case_id}/{folder}/{filename}
    file_name TEXT NOT NULL,

    -- Context
    case_id UUID,                         -- extracted from path (null for firm-wide reference files)
    firm_id UUID,                         -- extracted from path (for reference bucket)

    -- Routing (looked up from bucket_routing_criteria)
    pipeline TEXT NOT NULL,               -- 'full', 'embed-only', 'classify-then-route'
    phases INTEGER[] DEFAULT '{}',        -- [1,2,3] for full, [3] for embed-only
    priority TEXT DEFAULT 'medium',       -- 'immediate', 'high', 'medium', 'low'

    -- Lifecycle
    status TEXT DEFAULT 'pending',        -- pending, processing, routing, completed, failed, cancelled
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    -- Routing result (for intake-queue files that get rerouted)
    routed_to_bucket TEXT,                -- where the file was moved after classification
    routed_to_folder TEXT,
    classified_document_type TEXT,        -- what the classifier determined

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- Metadata
    file_size BIGINT,
    mime_type TEXT,
    storage_object_id UUID                -- reference to storage.objects.id
);

-- Indexes for the worker to poll efficiently
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_status ON pipeline_jobs(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_priority_created
    ON pipeline_jobs(priority, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_case_id ON pipeline_jobs(case_id);

-- ============================================================
-- FUNCTION: Create a pipeline job when a file is uploaded
-- ============================================================
CREATE OR REPLACE FUNCTION handle_storage_upload()
RETURNS TRIGGER AS $$
DECLARE
    v_bucket TEXT;
    v_folder TEXT;
    v_case_id UUID;
    v_firm_id UUID;
    v_file_name TEXT;
    v_path_parts TEXT[];
    v_routing RECORD;
BEGIN
    -- Only trigger on actual file uploads, not .criteria.json
    IF NEW.name LIKE '%.criteria.json' THEN
        RETURN NEW;
    END IF;

    v_bucket := NEW.bucket_id;

    -- Parse the path: {case_id_or_firm_id}/{folder}/{filename}
    v_path_parts := string_to_array(NEW.name, '/');

    -- Need at least 3 parts: id/folder/filename
    IF array_length(v_path_parts, 1) < 3 THEN
        -- File uploaded to root of bucket, put in unclassified
        v_folder := 'unclassified';
        v_file_name := NEW.name;
    ELSE
        v_folder := v_path_parts[2];
        v_file_name := v_path_parts[array_length(v_path_parts, 1)];

        -- Determine if first part is case_id or firm_id
        IF v_bucket = 'reference' THEN
            v_firm_id := v_path_parts[1]::UUID;
        ELSE
            v_case_id := v_path_parts[1]::UUID;
        END IF;
    END IF;

    -- Look up routing criteria
    SELECT pipeline, phases, priority
    INTO v_routing
    FROM bucket_routing_criteria
    WHERE bucket = v_bucket AND folder = v_folder
    LIMIT 1;

    -- Default if no criteria found
    IF v_routing IS NULL THEN
        v_routing.pipeline := 'classify-then-route';
        v_routing.phases := '{}';
        v_routing.priority := 'medium';
    END IF;

    -- Insert the job
    INSERT INTO pipeline_jobs (
        bucket, folder, file_path, file_name,
        case_id, firm_id,
        pipeline, phases, priority,
        status, file_size, mime_type, storage_object_id
    ) VALUES (
        v_bucket, v_folder, NEW.name, v_file_name,
        v_case_id, v_firm_id,
        v_routing.pipeline, v_routing.phases, v_routing.priority,
        'pending',
        (NEW.metadata->>'size')::BIGINT,
        NEW.metadata->>'mimetype',
        NEW.id
    );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================
-- TRIGGER: Fire on every storage upload
-- ============================================================
DROP TRIGGER IF EXISTS on_storage_upload ON storage.objects;
CREATE TRIGGER on_storage_upload
    AFTER INSERT ON storage.objects
    FOR EACH ROW
    EXECUTE FUNCTION handle_storage_upload();

-- ============================================================
-- FUNCTION: Claim the next pending job (for worker polling)
-- ============================================================
CREATE OR REPLACE FUNCTION claim_pipeline_job(p_pipeline_types TEXT[] DEFAULT ARRAY['full','embed-only','classify-then-route'])
RETURNS SETOF pipeline_jobs AS $$
    UPDATE pipeline_jobs
    SET status = 'processing',
        started_at = NOW()
    WHERE id = (
        SELECT id FROM pipeline_jobs
        WHERE status = 'pending'
          AND pipeline = ANY(p_pipeline_types)
          AND retry_count < max_retries
        ORDER BY
            CASE priority
                WHEN 'immediate' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
            END,
            created_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
$$ LANGUAGE sql;

-- ============================================================
-- FUNCTION: Complete or fail a job
-- ============================================================
CREATE OR REPLACE FUNCTION complete_pipeline_job(
    p_job_id UUID,
    p_status TEXT DEFAULT 'completed',
    p_error TEXT DEFAULT NULL,
    p_routed_bucket TEXT DEFAULT NULL,
    p_routed_folder TEXT DEFAULT NULL,
    p_doc_type TEXT DEFAULT NULL
)
RETURNS VOID AS $$
BEGIN
    UPDATE pipeline_jobs
    SET status = p_status,
        completed_at = CASE WHEN p_status IN ('completed','failed','cancelled') THEN NOW() ELSE completed_at END,
        error_message = p_error,
        routed_to_bucket = p_routed_bucket,
        routed_to_folder = p_routed_folder,
        classified_document_type = p_doc_type,
        retry_count = CASE WHEN p_status = 'failed' THEN retry_count + 1 ELSE retry_count END
    WHERE id = p_job_id;

    -- If failed but retries remaining, reset to pending
    UPDATE pipeline_jobs
    SET status = 'pending',
        started_at = NULL
    WHERE id = p_job_id
      AND status = 'failed'
      AND retry_count < max_retries;
END;
$$ LANGUAGE plpgsql;
