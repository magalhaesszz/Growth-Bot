import hashlib
import hmac
import json
import logging
import re
import uuid
from pathlib import Path

from instagrapi import Client
from instagrapi.mixins.bloks import (
    AP_2SV_CODE_ENTRY,
    AP_2SV_CODE_ENTRY_ASYNC,
    AP_2SV_ENTRYPOINT,
)
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    TwoFactorRequired,
)

from config import (
    INSTAGRAM_COUNTRY,
    INSTAGRAM_COUNTRY_CODE,
    INSTAGRAM_LOCALE,
    INSTAGRAM_PROXY,
    INSTAGRAM_TIMEZONE_OFFSET,
    SESSION_ENCRYPTION_KEY,
    SESSIONS_DIR,
)

logger = logging.getLogger(__name__)

# {username: {"client": InstagramClient, "type": "caa"|"2fa"}}
PENDING_CHALLENGES: dict[str, dict] = {}


def _normalize_code(code: str) -> str:
    return re.sub(r"[\s\-]+", "", str(code).strip())


def _detect_code_type(code: str) -> str:
    clean = _normalize_code(code)
    if re.fullmatch(r"\d{8}", clean):
        return "backup"
    if re.fullmatch(r"\d{6}", clean):
        return "sms_or_totp"
    return "unknown"


def _format_preview(code: str) -> str:
    clean = _normalize_code(code)
    if len(clean) == 8:
        return f"{clean[:4]}-{clean[4:]}"
    if len(clean) == 6:
        return f"{clean[:3]}-{clean[3:]}"
    return clean


class InstagramClient:
    """Cliente com perfil de aplicativo atual e identidade estável por conta."""

    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self._fingerprint = {"device": "Pixel 8 Pro (padrão instagrapi)"}
        self._caa_send_result: dict | None = None
        self._caa_submit_context: dict | None = None
        self._verification_mode: str | None = None
        self.cl = self._build_client()

    def _stable_bytes(self, label: str) -> bytes:
        key = SESSION_ENCRYPTION_KEY.encode("utf-8")
        value = f"instagram:{self.username.casefold()}:{label}".encode("utf-8")
        return hmac.new(key, value, hashlib.sha256).digest()

    def _stable_uuid(self, label: str) -> str:
        return str(uuid.UUID(bytes=self._stable_bytes(label)[:16], version=4))

    def _apply_network_identity(self, client: Client) -> None:
        if INSTAGRAM_PROXY:
            client.set_proxy(INSTAGRAM_PROXY)
        client.set_country(INSTAGRAM_COUNTRY)
        client.set_country_code(INSTAGRAM_COUNTRY_CODE)
        client.set_locale(INSTAGRAM_LOCALE)
        client.set_timezone_offset(INSTAGRAM_TIMEZONE_OFFSET)

    def _build_client(self) -> Client:
        client = Client()
        # Preserva o perfil de aplicativo padrão da versão instalada.
        client.set_uuids(
            {
                "phone_id": self._stable_uuid("phone_id"),
                "uuid": self._stable_uuid("uuid"),
                "client_session_id": self._stable_uuid("client_session_id"),
                "advertising_id": self._stable_uuid("advertising_id"),
                "android_device_id": "android-"
                + self._stable_bytes("android_device_id").hex()[:16],
                "request_id": self._stable_uuid("request_id"),
                "tray_session_id": self._stable_uuid("tray_session_id"),
            }
        )
        self._apply_network_identity(client)
        client.challenge_code_handler = self._telegram_code_handler
        return client

    def randomize_fingerprint(self):
        # Nome antigo mantido por compatibilidade; a identidade não gira mais.
        self.cl = self._build_client()
        return self._fingerprint

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """Nunca bloqueia uma thread aguardando entrada interativa."""
        PENDING_CHALLENGES[username] = {"client": self, "type": "challenge"}
        raise ChallengeRequired("verification_code_required")

    def login(self) -> str:
        if self.session_path.exists():
            try:
                logger.info("[%s] Restaurando sessão salva.", self.username)
                self.cl.load_settings(str(self.session_path))
                self._apply_network_identity(self.cl)
                self.cl.challenge_code_handler = self._telegram_code_handler
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                self._save_session()
                return "ok"
            except LoginRequired:
                logger.warning("[%s] Sessão expirada.", self.username)
            except (ChallengeRequired, TwoFactorRequired):
                logger.info("[%s] Sessão requer nova verificação.", self.username)
            except Exception as exc:
                logger.warning(
                    "[%s] Falha ao restaurar sessão: %s",
                    self.username,
                    type(exc).__name__,
                )
            self.cl = self._build_client()

        return self._caa_login()

    def _caa_login(self, verification_code: str = "") -> str:
        """Inicia CAA e retorna ao Telegram antes de solicitar o código."""
        try:
            if verification_code and self._caa_send_result:
                return "ok" if self._submit_caa_code(verification_code) else "error:invalid_code"

            if not self.cl.bloks_caa_login_prepare(username=self.username):
                return "error:caa_preflight"

            result = self.cl.bloks_caa_login_send_request(
                self.password,
                username=self.username,
            )
            if self.cl.bloks_apply_login_response(result):
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return "ok"

            if self.cl.bloks_caa_login_needs_two_step(result):
                self._caa_send_result = result
                if not self._prepare_caa_challenge(result):
                    return "error:caa_challenge_prepare"
                self._verification_mode = "caa"
                PENDING_CHALLENGES[self.username] = {
                    "client": self,
                    "type": "caa",
                }
                return "challenge"
            return "error:caa_failed"

        except TwoFactorRequired:
            self._verification_mode = "2fa"
            PENDING_CHALLENGES[self.username] = {
                "client": self,
                "type": "2fa",
            }
            return "two_factor"
        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"
        except BadPassword:
            return "error:bad_password"
        except FeedbackRequired:
            return "error:feedback_required"
        except Exception as exc:
            message = str(exc).lower()
            if "429" in message or "too many" in message or "retry" in message:
                return "error:rate_limit_429"
            logger.error("[%s] Erro CAA: %s", self.username, type(exc).__name__)
            return f"error:{type(exc).__name__}"

    def start_challenge_with_method(self, method_type: str) -> str:
        # O CAA atual escolhe o canal no servidor; não oferece seletor SMS/email.
        return "challenge" if self._caa_send_result else self._caa_login()

    def _submit_caa_code(self, code: str) -> bool:
        clean = _normalize_code(code)
        if not self._caa_submit_context or _detect_code_type(clean) == "unknown":
            return False
        try:
            result = self.cl.bloks_ap_two_step_verification_submit_code(
                self._caa_submit_context,
                clean,
            )
            if self.cl.bloks_apply_login_response(result):
                self._save_session()
                self._caa_send_result = None
                self._caa_submit_context = None
                self._verification_mode = None
                PENDING_CHALLENGES.pop(self.username, None)
                return True
        except Exception as exc:
            logger.warning(
                "[%s] Código CAA rejeitado: %s",
                self.username,
                type(exc).__name__,
            )
        return False

    def _prepare_caa_challenge(self, send_result: dict) -> bool:
        """Solicita o código ao Instagram e guarda somente a etapa de envio."""
        entry_context = self.cl.bloks_extract_context_data(
            send_result,
            AP_2SV_ENTRYPOINT,
        )
        if not entry_context:
            return False
        entry_result = self.cl.bloks_ap_two_step_verification_entrypoint(entry_context)
        code_context = self.cl.bloks_extract_context_data(
            entry_result,
            AP_2SV_CODE_ENTRY,
        )
        if not code_context:
            return False
        code_result = self.cl.bloks_ap_two_step_verification_code_entry(code_context)
        self._caa_submit_context = self.cl.bloks_extract_context_data(
            code_result,
            AP_2SV_CODE_ENTRY_ASYNC,
        )
        return bool(self._caa_submit_context)

    def submit_code(self, code: str) -> str:
        return "ok" if self._submit_caa_code(code) else "error"

    def submit_backup_code(self, code: str) -> bool:
        clean = _normalize_code(code)
        return len(clean) == 8 and self._submit_caa_code(clean)

    def submit_2fa(self, code: str) -> bool:
        clean = _normalize_code(code)
        if _detect_code_type(clean) == "unknown":
            return False
        if self._caa_send_result:
            return self._submit_caa_code(clean)
        try:
            logged = self.cl.login(
                self.username,
                self.password,
                verification_code=clean,
            )
            if logged:
                self._save_session()
                self._verification_mode = None
                PENDING_CHALLENGES.pop(self.username, None)
                return True
        except Exception as exc:
            logger.warning(
                "[%s] Código 2FA rejeitado: %s",
                self.username,
                type(exc).__name__,
            )
        return False

    def _save_session(self):
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.cl.dump_settings(str(self.session_path))

    def save_session(self):
        self._save_session()

    def get_session_data(self) -> dict:
        if self.session_path.exists():
            with self.session_path.open(encoding="utf-8") as session_file:
                return json.load(session_file)
        return {}

    def load_session_from_data(self, data: dict):
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session_path.open("w", encoding="utf-8") as session_file:
            json.dump(data, session_file)
        self.cl.load_settings(str(self.session_path))
        self._apply_network_identity(self.cl)
        self.cl.challenge_code_handler = self._telegram_code_handler

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        return self.is_logged_in() or self.login() == "ok"

    @property
    def api(self) -> Client:
        return self.cl


def detect_code_type(code: str) -> str:
    return _detect_code_type(code)


def format_preview(code: str) -> str:
    return _format_preview(code)
