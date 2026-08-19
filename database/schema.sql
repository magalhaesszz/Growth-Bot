-- Growth Bot - schema completo para novas instalacoes.
-- Execute uma vez no SQL Editor do Supabase antes de iniciar o bot.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ig_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT UNIQUE NOT NULL,
    password_enc TEXT NOT NULL,
    session_data JSONB,
    fingerprint JSONB,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'banned', 'warming')),
    warmup_day INTEGER NOT NULL DEFAULT 0 CHECK (warmup_day >= 0),
    daily_follows INTEGER NOT NULL DEFAULT 40 CHECK (daily_follows BETWEEN 0 AND 200),
    daily_unfollows INTEGER NOT NULL DEFAULT 40 CHECK (daily_unfollows BETWEEN 0 AND 200),
    hour_start INTEGER NOT NULL DEFAULT 8 CHECK (hour_start BETWEEN 0 AND 23),
    hour_end INTEGER NOT NULL DEFAULT 22 CHECK (hour_end BETWEEN 1 AND 24),
    delay_min INTEGER NOT NULL DEFAULT 30 CHECK (delay_min BETWEEN 5 AND 600),
    delay_max INTEGER NOT NULL DEFAULT 90 CHECK (delay_max BETWEEN 5 AND 600),
    score_min INTEGER NOT NULL DEFAULT 50 CHECK (score_min BETWEEN 0 AND 100),
    unfollow_after_days INTEGER NOT NULL DEFAULT 5 CHECK (unfollow_after_days BETWEEN 1 AND 365),
    unfollow_policy TEXT NOT NULL DEFAULT 'keep_follow_backs'
        CHECK (unfollow_policy IN ('remove_all', 'keep_follow_backs', 'remove_only_follow_backs')),
    daily_report_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ,
    CHECK (hour_start < hour_end),
    CHECK (delay_min <= delay_max)
);

CREATE TABLE IF NOT EXISTS ig_campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    nicho TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    total_follows INTEGER NOT NULL DEFAULT 0,
    total_unfollows INTEGER NOT NULL DEFAULT 0,
    total_follow_backs INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS ig_followed (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    target_user_id TEXT NOT NULL,
    target_username TEXT NOT NULL,
    campaign_id UUID REFERENCES ig_campaigns(id) ON DELETE SET NULL,
    followed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    unfollowed_at TIMESTAMPTZ,
    follows_back BOOLEAN NOT NULL DEFAULT FALSE,
    score INTEGER,
    status TEXT NOT NULL DEFAULT 'following'
        CHECK (status IN ('following', 'unfollowed', 'whitelisted')),
    UNIQUE (account_id, target_user_id)
);

CREATE TABLE IF NOT EXISTS ig_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    page_url TEXT NOT NULL,
    page_username TEXT,
    page_user_id TEXT,
    priority INTEGER NOT NULL DEFAULT 1,
    campaign_id UUID REFERENCES ig_campaigns(id) ON DELETE SET NULL,
    scraped_count INTEGER NOT NULL DEFAULT 0,
    last_scraped_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ig_whitelist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    target_username TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, target_username)
);

CREATE TABLE IF NOT EXISTS ig_blacklist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'username' CHECK (type IN ('username', 'keyword')),
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, term, type)
);

CREATE TABLE IF NOT EXISTS ig_action_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    target_username TEXT,
    detail TEXT,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ig_action_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload JSONB NOT NULL,
    retries INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed')),
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

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, storage_path)
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, slug)
);

CREATE TABLE IF NOT EXISTS video_settings (
    user_id BIGINT PRIMARY KEY,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

-- Bucket privado/publico deve seguir a politica do projeto no Supabase Storage.
-- O codigo usa o bucket chamado "videos"; crie-o no painel caso ainda nao exista.
