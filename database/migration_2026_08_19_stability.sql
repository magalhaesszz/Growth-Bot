-- Growth Bot - migracao idempotente de estabilidade (bancos existentes).
-- Pode ser executada mais de uma vez no SQL Editor do Supabase.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE IF EXISTS ig_accounts
    ADD COLUMN IF NOT EXISTS unfollow_policy TEXT DEFAULT 'keep_follow_backs',
    ADD COLUMN IF NOT EXISTS daily_report_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE IF EXISTS ig_accounts
    DROP CONSTRAINT IF EXISTS ig_accounts_unfollow_policy_check;
ALTER TABLE IF EXISTS ig_accounts
    ADD CONSTRAINT ig_accounts_unfollow_policy_check
    CHECK (unfollow_policy IN ('remove_all', 'keep_follow_backs', 'remove_only_follow_backs'));

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS video_settings (
    user_id BIGINT PRIMARY KEY,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL,
    filename TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL,
    size_mb DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'downloaded',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS config_fundo (
    user_id BIGINT PRIMARY KEY,
    storage_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS config_fundos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL,
    slug TEXT NOT NULL,
    nome TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_id BIGINT NOT NULL,
    actor_username TEXT NOT NULL DEFAULT '?',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_users (
    user_id BIGINT PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '?',
    name TEXT NOT NULL DEFAULT '?',
    added_at TEXT,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Nao adicionamos UNIQUE automaticamente a tabelas historicas que podem conter
-- duplicatas antigas. O codigo novo e idempotente e novas instalacoes recebem
-- as restricoes completas via database/schema.sql.
CREATE INDEX IF NOT EXISTS idx_ig_followed_account_status
    ON ig_followed(account_id, status);
CREATE INDEX IF NOT EXISTS idx_ig_followed_followed_at
    ON ig_followed(account_id, followed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ig_targets_account_status
    ON ig_targets(account_id, status);
CREATE INDEX IF NOT EXISTS idx_ig_action_logs_account_time
    ON ig_action_logs(account_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ig_action_logs_account_action_time
    ON ig_action_logs(account_id, action, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ig_action_queue_pending
    ON ig_action_queue(account_id, status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_videos_user_created
    ON videos(user_id, created_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'config_fundos_user_slug_key'
    ) THEN
        BEGIN
            ALTER TABLE config_fundos
                ADD CONSTRAINT config_fundos_user_slug_key UNIQUE (user_id, slug);
        EXCEPTION WHEN unique_violation THEN
            RAISE NOTICE 'config_fundos possui duplicatas; UNIQUE nao foi adicionada.';
        END;
    END IF;
END $$;
