-- Migration 006: Connected channels + API keys for CMS webhooks
-- PRE-REQUISITE: Migrations 001-005

CREATE TABLE IF NOT EXISTS connected_channels (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id         UUID NOT NULL REFERENCES firms(id),
    channel_type    TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    is_active       BOOLEAN NOT NULL DEFAULT true,
    default_priority    TEXT NOT NULL DEFAULT 'overnight',
    default_case_id     UUID REFERENCES cases(id),
    default_corpus_id   UUID REFERENCES corpus(id),
    last_sync_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_connected_channels_firm
    ON connected_channels (firm_id, channel_type) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS channel_api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id  UUID NOT NULL REFERENCES connected_channels(id) ON DELETE CASCADE,
    firm_id     UUID NOT NULL REFERENCES firms(id),
    key_hash    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    is_active   BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_channel_api_keys_hash
    ON channel_api_keys (key_hash) WHERE is_active = true;

-- RLS
ALTER TABLE connected_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE connected_channels FORCE ROW LEVEL SECURITY;
CREATE POLICY cc_select ON connected_channels FOR SELECT USING (firm_id = public.get_firm_id());
CREATE POLICY cc_insert ON connected_channels FOR INSERT WITH CHECK (firm_id = public.get_firm_id());
CREATE POLICY cc_update ON connected_channels FOR UPDATE USING (firm_id = public.get_firm_id()) WITH CHECK (firm_id = public.get_firm_id());
CREATE POLICY cc_delete ON connected_channels FOR DELETE USING (firm_id = public.get_firm_id());

ALTER TABLE channel_api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY cak_select ON channel_api_keys FOR SELECT USING (firm_id = public.get_firm_id());
CREATE POLICY cak_insert ON channel_api_keys FOR INSERT WITH CHECK (firm_id = public.get_firm_id());
CREATE POLICY cak_delete ON channel_api_keys FOR DELETE USING (firm_id = public.get_firm_id());
