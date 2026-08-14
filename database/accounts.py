import json
import logging
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, SESSION_ENCRYPTION_KEY

logger = logging.getLogger(__name__)

# ─── SQL: rode no Supabase SQL Editor para criar as tabelas ──
SCHEMA_SQL = """
-- Contas Instagram gerenciadas
CREATE TABLE IF NOT EXISTS ig_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT UNIQUE NOT NULL,
    password_enc    TEXT NOT NULL,             -- senha criptografada
    session_data    JSONB,                     -- sessão criptografada (backup)
    fingerprint     JSONB,                     -- dispositivo simulado
    status          TEXT DEFAULT 'active',     -- active | paused | banned | warming
    warmup_day      INTEGER DEFAULT 0,         -- dia atual do aquecimento (0 = fora)
    daily_follows   INTEGER DEFAULT 40,
    daily_unfollows INTEGER DEFAULT 40,
    hour_start      INTEGER DEFAULT 8,
    hour_end        INTEGER DEFAULT 22,
    delay_min       INTEGER DEFAULT 30,
    delay_max       INTEGER DEFAULT 90,
    score_min       INTEGER DEFAULT 50,
    unfollow_after_days INTEGER DEFAULT 5,
    unfollow_policy TEXT DEFAULT 'keep_follow_backs',
    daily_report_enabled BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_active_at  TIMESTAMPTZ
);

-- Perfis seguidos por cada conta
CREATE TABLE IF NOT EXISTS ig_followed (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    target_user_id  TEXT NOT NULL,
    target_username TEXT NOT NULL,
    campaign_id     UUID,
    followed_at     TIMESTAMPTZ DEFAULT now(),
    unfollowed_at   TIMESTAMPTZ,
    follows_back    BOOLEAN DEFAULT FALSE,
    score           INTEGER,
    status          TEXT DEFAULT 'following'   -- following | unfollowed | whitelisted
);

-- Páginas-alvo de scraping
CREATE TABLE IF NOT EXISTS ig_targets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    page_url        TEXT NOT NULL,
    page_username   TEXT,
    page_user_id    TEXT,
    priority        INTEGER DEFAULT 1,
    campaign_id     UUID,
    scraped_count   INTEGER DEFAULT 0,
    last_scraped_at TIMESTAMPTZ,
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Campanhas (agrupamento de períodos de ação)
CREATE TABLE IF NOT EXISTS ig_campaigns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    nicho           TEXT,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    total_follows   INTEGER DEFAULT 0,
    total_unfollows INTEGER DEFAULT 0,
    total_follow_backs INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'active'
);

-- Whitelist (nunca deixar de seguir)
CREATE TABLE IF NOT EXISTS ig_whitelist (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    target_username TEXT NOT NULL,
    added_at        TIMESTAMPTZ DEFAULT now()
);

-- Blacklist (nunca seguir)
CREATE TABLE IF NOT EXISTS ig_blacklist (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    term            TEXT NOT NULL,             -- username ou palavra-chave
    type            TEXT DEFAULT 'username',   -- username | keyword
    added_at        TIMESTAMPTZ DEFAULT now()
);

-- Log de ações
CREATE TABLE IF NOT EXISTS ig_action_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,             -- follow | unfollow | story_view | error
    target_username TEXT,
    detail          TEXT,
    success         BOOLEAN DEFAULT TRUE,
    executed_at     TIMESTAMPTZ DEFAULT now()
);

-- Usuarios autorizados no bot Telegram
CREATE TABLE IF NOT EXISTS bot_users (
    user_id     BIGINT PRIMARY KEY,
    username    TEXT DEFAULT '?',
    name        TEXT DEFAULT '?',
    added_at    TEXT,
    is_admin    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Fila de ações com retry
CREATE TABLE IF NOT EXISTS ig_action_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES ig_accounts(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    payload         JSONB NOT NULL,
    retries         INTEGER DEFAULT 0,
    next_attempt_at TIMESTAMPTZ DEFAULT now(),
    status          TEXT DEFAULT 'pending',    -- pending | processing | done | failed
    created_at      TIMESTAMPTZ DEFAULT now()
);
"""


def _fernet() -> Fernet:
    return Fernet(SESSION_ENCRYPTION_KEY.encode())


def _encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


class AccountsDB:
    def __init__(self):
        self.sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ─── CRUD contas ─────────────────────────────────────────

    def add_account(self, username: str, password: str, fingerprint: dict = None) -> dict:
        data = {
            "username": username,
            "password_enc": _encrypt(password),
            "fingerprint": fingerprint,
            "status": "warming",
            "warmup_day": 1,
        }
        existing = self.sb.table("ig_accounts").select("id").eq(
            "username", username
        ).limit(1).execute()
        if existing.data:
            res = self.sb.table("ig_accounts").update({
                "password_enc": data["password_enc"],
                "fingerprint": fingerprint,
            }).eq("username", username).execute()
            logger.info("Credenciais atualizadas para: %s", username)
        else:
            res = self.sb.table("ig_accounts").insert(data).execute()
            logger.info("Conta adicionada: %s", username)
        return res.data[0] if res.data else {}

    def get_account(self, username: str) -> dict | None:
        res = (
            self.sb.table("ig_accounts")
            .select("*")
            .eq("username", username)
            .execute()
        )
        if res.data:
            row = res.data[0]
            row["password"] = _decrypt(row["password_enc"])
            return row
        return None

    def list_accounts(self) -> list[dict]:
        res = self.sb.table("ig_accounts").select("*").execute()
        accounts = []
        for row in res.data:
            row["password"] = _decrypt(row["password_enc"])
            accounts.append(row)
        return accounts

    def list_active_accounts(self) -> list[dict]:
        res = (
            self.sb.table("ig_accounts")
            .select("*")
            .in_("status", ["active", "warming"])
            .execute()
        )
        accounts = []
        for row in res.data:
            row["password"] = _decrypt(row["password_enc"])
            accounts.append(row)
        return accounts

    def update_status(self, username: str, status: str):
        self.sb.table("ig_accounts").update({"status": status}).eq("username", username).execute()

    def update_last_active(self, username: str):
        self.sb.table("ig_accounts").update(
            {"last_active_at": datetime.now(timezone.utc).isoformat()}
        ).eq("username", username).execute()

    def update_settings(self, username: str, settings: dict):
        allowed = {
            "daily_follows", "daily_unfollows", "hour_start", "hour_end",
            "delay_min", "delay_max", "score_min", "unfollow_after_days",
            "unfollow_policy", "daily_report_enabled",
        }
        payload = {k: v for k, v in settings.items() if k in allowed}
        self.sb.table("ig_accounts").update(payload).eq("username", username).execute()

    def remove_account(self, username: str):
        self.sb.table("ig_accounts").delete().eq("username", username).execute()
        logger.info(f"Conta removida: {username}")

    # ─── Backup de sessão ────────────────────────────────────

    def save_session_backup(self, username: str, session_data: dict):
        encrypted = _encrypt(json.dumps(session_data))
        self.sb.table("ig_accounts").update(
            {"session_data": {"enc": encrypted}}
        ).eq("username", username).execute()
        logger.debug(f"[{username}] Sessão salva no Supabase.")

    def load_session_backup(self, username: str) -> dict | None:
        res = (
            self.sb.table("ig_accounts")
            .select("session_data")
            .eq("username", username)
            .execute()
        )
        if res.data and res.data[0].get("session_data"):
            enc = res.data[0]["session_data"].get("enc")
            if enc:
                return json.loads(_decrypt(enc))
        return None

    # ─── Aquecimento ─────────────────────────────────────────

    def advance_warmup_day(self, username: str) -> int:
        acc = self.get_account(username)
        if not acc:
            return 0
        next_day = acc.get("warmup_day", 1) + 1
        self.sb.table("ig_accounts").update({"warmup_day": next_day}).eq("username", username).execute()
        return next_day

    def finish_warmup(self, username: str):
        self.sb.table("ig_accounts").update(
            {"warmup_day": 0, "status": "active"}
        ).eq("username", username).execute()
        logger.info(f"[{username}] Aquecimento concluído. Conta ativa.")
