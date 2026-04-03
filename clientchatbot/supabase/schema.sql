-- ============================================================
-- CLIENT CHATBOT SCHEMA
-- Run this in your Supabase SQL editor to create the tables.
-- These are SEPARATE from the existing TFG tables.
-- ============================================================

-- ─── ENUMS ──────────────────────────────────────────────────

CREATE TYPE case_status AS ENUM (
    'intake', 'active', 'pending_documents', 'under_review',
    'closed', 'archived'
);

CREATE TYPE practice_area AS ENUM (
    'commercial_litigation', 'immigration'
);

CREATE TYPE conversation_channel AS ENUM (
    'website', 'whatsapp', 'email'
);

CREATE TYPE message_role AS ENUM (
    'user', 'assistant', 'system', 'tool'
);

CREATE TYPE review_status AS ENUM (
    'not_required', 'pending_review', 'approved', 'rejected', 'edited'
);


-- ─── CLIENTS ────────────────────────────────────────────────

CREATE TABLE chatbot_clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    phone           TEXT,
    first_name      TEXT,
    last_name       TEXT,
    preferred_lang  TEXT DEFAULT 'en',
    channel         TEXT DEFAULT 'website',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_clients_email ON chatbot_clients(email);
CREATE INDEX idx_chatbot_clients_phone ON chatbot_clients(phone);


-- ─── CASES ──────────────────────────────────────────────────

CREATE TABLE chatbot_cases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES chatbot_clients(id),
    practice_area   practice_area NOT NULL,
    case_status     case_status DEFAULT 'intake',
    title           TEXT,
    description     TEXT,
    assigned_lawyer TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_cases_client ON chatbot_cases(client_id);
CREATE INDEX idx_chatbot_cases_status ON chatbot_cases(case_status);


-- ─── CONVERSATIONS ──────────────────────────────────────────

CREATE TABLE chatbot_conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES chatbot_clients(id),
    case_id         UUID REFERENCES chatbot_cases(id),
    channel         conversation_channel DEFAULT 'website',
    practice_area   practice_area NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_conversations_client ON chatbot_conversations(client_id);
CREATE INDEX idx_chatbot_conversations_case ON chatbot_conversations(case_id);


-- ─── MESSAGES ───────────────────────────────────────────────

CREATE TABLE chatbot_messages (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID NOT NULL REFERENCES chatbot_conversations(id),
    role              message_role NOT NULL,
    content           TEXT NOT NULL,
    tool_name         TEXT,
    tool_args         JSONB,
    tool_result       JSONB,
    requires_review   BOOLEAN DEFAULT false,
    review_status     review_status DEFAULT 'not_required',
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    edited_content    TEXT,
    ai_provider       TEXT,
    ai_model          TEXT,
    token_count       INTEGER,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_messages_conversation ON chatbot_messages(conversation_id);
CREATE INDEX idx_chatbot_messages_review ON chatbot_messages(requires_review, review_status)
    WHERE requires_review = true;


-- ─── FILES (for future WhatsApp/dashboard uploads) ──────────

CREATE TABLE chatbot_files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID REFERENCES chatbot_clients(id),
    case_id         UUID REFERENCES chatbot_cases(id),
    conversation_id UUID REFERENCES chatbot_conversations(id),
    file_name       TEXT NOT NULL,
    file_type       TEXT,
    file_size       INTEGER,
    storage_path    TEXT NOT NULL,
    uploaded_by     TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_files_case ON chatbot_files(case_id);


-- ─── KNOWLEDGE BASE ─────────────────────────────────────────

CREATE TABLE chatbot_knowledge_base (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    practice_area   practice_area NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    category        TEXT,
    metadata        JSONB DEFAULT '{}',
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chatbot_kb_practice ON chatbot_knowledge_base(practice_area)
    WHERE is_active;


-- ─── AUTO-UPDATE TIMESTAMPS ─────────────────────────────────

CREATE OR REPLACE FUNCTION chatbot_update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_chatbot_clients_updated
    BEFORE UPDATE ON chatbot_clients
    FOR EACH ROW EXECUTE FUNCTION chatbot_update_updated_at();

CREATE TRIGGER tr_chatbot_cases_updated
    BEFORE UPDATE ON chatbot_cases
    FOR EACH ROW EXECUTE FUNCTION chatbot_update_updated_at();

CREATE TRIGGER tr_chatbot_kb_updated
    BEFORE UPDATE ON chatbot_knowledge_base
    FOR EACH ROW EXECUTE FUNCTION chatbot_update_updated_at();
