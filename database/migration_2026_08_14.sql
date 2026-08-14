-- Execute uma vez no SQL Editor do Supabase para bancos já existentes.
ALTER TABLE ig_accounts
    ADD COLUMN IF NOT EXISTS unfollow_policy TEXT DEFAULT 'keep_follow_backs',
    ADD COLUMN IF NOT EXISTS daily_report_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE ig_accounts
    DROP CONSTRAINT IF EXISTS ig_accounts_unfollow_policy_check;

ALTER TABLE ig_accounts
    ADD CONSTRAINT ig_accounts_unfollow_policy_check
    CHECK (unfollow_policy IN (
        'remove_all',
        'keep_follow_backs',
        'remove_only_follow_backs'
    ));

CREATE TABLE IF NOT EXISTS video_settings (
    user_id BIGINT PRIMARY KEY,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
