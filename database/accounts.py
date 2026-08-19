import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from supabase import Client, create_client

from config import SESSION_ENCRYPTION_KEY, SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
try:
    SCHEMA_SQL = _SCHEMA_PATH.read_text(encoding="utf-8")
except OSError:
    SCHEMA_SQL = ""

_ALLOWED_STATUSES = {"active", "paused", "banned", "warming"}
_ALLOWED_POLICIES = {"remove_all", "keep_follow_backs", "remove_only_follow_backs"}


def _fernet() -> Fernet:
    return Fernet(SESSION_ENCRYPTION_KEY.encode())


def _encrypt(text: str) -> str:
    return _fernet().encrypt(str(text).encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def _validate_combined_settings(settings: dict) -> dict:
    """Valida configuracoes em um unico ponto para comandos e painel."""
    data = dict(settings)

    integer_ranges = {
        "daily_follows": (0, 200),
        "daily_unfollows": (0, 200),
        "hour_start": (0, 23),
        "hour_end": (1, 24),
        "delay_min": (5, 600),
        "delay_max": (5, 600),
        "score_min": (0, 100),
        "unfollow_after_days": (1, 365),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} deve ser numero inteiro")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} deve ser numero inteiro") from exc
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{key} deve estar entre {minimum} e {maximum}")
        data[key] = parsed

    if data.get("hour_start", 8) >= data.get("hour_end", 22):
        raise ValueError("hour_start deve ser menor que hour_end")
    if data.get("delay_min", 30) > data.get("delay_max", 90):
        raise ValueError("delay_min nao pode ser maior que delay_max")

    if "unfollow_policy" in data:
        policy = str(data["unfollow_policy"])
        if policy not in _ALLOWED_POLICIES:
            raise ValueError("unfollow_policy invalida")
        data["unfollow_policy"] = policy

    if "daily_report_enabled" in data:
        value = data["daily_report_enabled"]
        if not isinstance(value, bool):
            raise ValueError("daily_report_enabled deve ser booleano")

    return data


class AccountsDB:
    def __init__(self):
        self.sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def _decode_row(self, row: dict) -> dict:
        item = dict(row)
        token = item.get("password_enc")
        if token:
            try:
                item["password"] = _decrypt(token)
            except (InvalidToken, ValueError, TypeError):
                logger.error(
                    "Nao foi possivel descriptografar a senha de @%s.",
                    item.get("username", "?"),
                )
                item["password"] = ""
        else:
            item["password"] = ""
        return item

    # ─── CRUD contas ─────────────────────────────────────────

    def add_account(
        self, username: str, password: str, fingerprint: dict | None = None
    ) -> dict:
        username = username.strip().lstrip("@")
        existing = (
            self.sb.table("ig_accounts")
            .select("*")
            .eq("username", username)
            .limit(1)
            .execute()
        )
        if existing.data:
            # SESSIONID novo nao pode apagar uma senha que ja estava armazenada.
            payload = {}
            if password:
                payload["password_enc"] = _encrypt(password)
            if fingerprint is not None:
                payload["fingerprint"] = fingerprint
            if payload:
                res = (
                    self.sb.table("ig_accounts")
                    .update(payload)
                    .eq("username", username)
                    .execute()
                )
                row = res.data[0] if res.data else {**existing.data[0], **payload}
            else:
                row = existing.data[0]
            logger.info("Conta atualizada sem resetar estado: %s", username)
            return self._decode_row(row)

        data = {
            "username": username,
            "password_enc": _encrypt(password or ""),
            "fingerprint": fingerprint,
            "status": "warming",
            "warmup_day": 1,
        }
        res = self.sb.table("ig_accounts").insert(data).execute()
        logger.info("Conta adicionada: %s", username)
        return self._decode_row(res.data[0]) if res.data else {}

    def get_account_by_id(self, account_id: str) -> dict | None:
        res = (
            self.sb.table("ig_accounts")
            .select("*")
            .eq("id", account_id)
            .limit(1)
            .execute()
        )
        return self._decode_row(res.data[0]) if res.data else None

    def get_account(self, username: str) -> dict | None:
        res = (
            self.sb.table("ig_accounts")
            .select("*")
            .eq("username", username.strip().lstrip("@"))
            .limit(1)
            .execute()
        )
        return self._decode_row(res.data[0]) if res.data else None

    def list_accounts(self) -> list[dict]:
        res = self.sb.table("ig_accounts").select("*").execute()
        return [self._decode_row(row) for row in (res.data or [])]

    def list_active_accounts(self) -> list[dict]:
        res = (
            self.sb.table("ig_accounts")
            .select("*")
            .in_("status", ["active", "warming"])
            .execute()
        )
        return [self._decode_row(row) for row in (res.data or [])]

    def update_status(self, username: str, status: str):
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"Status invalido: {status}")
        username = username.strip().lstrip("@")
        self.sb.table("ig_accounts").update({"status": status}).eq(
            "username", username
        ).execute()
        # Um comando explicito para voltar a 'active' tambem deve limpar a
        # pausa em memoria/persistida do detector de risco.
        if status == "active":
            try:
                from instagram.risk_detector import risk_detector

                risk_detector.resume(username)
            except Exception as exc:
                logger.warning(
                    "[%s] Status ativo salvo, mas risco nao foi limpo: %s",
                    username,
                    type(exc).__name__,
                )

    def update_last_active(self, username: str):
        self.sb.table("ig_accounts").update(
            {"last_active_at": datetime.now(timezone.utc).isoformat()}
        ).eq("username", username).execute()

    def update_settings(self, username: str, settings: dict):
        allowed = {
            "daily_follows",
            "daily_unfollows",
            "hour_start",
            "hour_end",
            "delay_min",
            "delay_max",
            "score_min",
            "unfollow_after_days",
            "unfollow_policy",
            "daily_report_enabled",
        }
        requested = {key: value for key, value in settings.items() if key in allowed}
        if not requested:
            return

        current = self.get_account(username)
        if not current:
            raise ValueError("Conta nao encontrada")
        combined = {key: current.get(key) for key in allowed}
        combined.update(requested)
        validated = _validate_combined_settings(combined)
        payload = {key: validated[key] for key in requested}
        self.sb.table("ig_accounts").update(payload).eq(
            "username", username.strip().lstrip("@")
        ).execute()

    def remove_account(self, username: str):
        username = username.strip().lstrip("@")
        self.sb.table("ig_accounts").delete().eq("username", username).execute()
        logger.info("Conta removida: %s", username)

    # ─── Backup de sessao ────────────────────────────────────

    def save_session_backup(self, username: str, session_data: dict):
        encrypted = _encrypt(json.dumps(session_data, separators=(",", ":")))
        self.sb.table("ig_accounts").update(
            {"session_data": {"enc": encrypted}}
        ).eq("username", username).execute()
        logger.debug("[%s] Sessao salva no Supabase.", username)

    def load_session_backup(self, username: str) -> dict | None:
        res = (
            self.sb.table("ig_accounts")
            .select("session_data")
            .eq("username", username)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("session_data"):
            enc = res.data[0]["session_data"].get("enc")
            if enc:
                try:
                    return json.loads(_decrypt(enc))
                except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
                    logger.error(
                        "[%s] Backup de sessao corrompido/incompativel: %s",
                        username,
                        type(exc).__name__,
                    )
        return None

    # ─── Aquecimento ─────────────────────────────────────────

    def advance_warmup_day(self, username: str) -> int:
        acc = self.get_account(username)
        if not acc:
            return 0
        next_day = int(acc.get("warmup_day", 1) or 1) + 1
        self.sb.table("ig_accounts").update({"warmup_day": next_day}).eq(
            "username", username
        ).execute()
        return next_day

    def finish_warmup(self, username: str):
        self.sb.table("ig_accounts").update(
            {"warmup_day": 0, "status": "active"}
        ).eq("username", username).execute()
        logger.info("[%s] Aquecimento concluido. Conta ativa.", username)
