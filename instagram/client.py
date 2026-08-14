import hashlib
import hmac
import json
import logging
import re
import uuid
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    ReloginAttemptExceeded,
    TwoFactorRequired,
)
from instagrapi.mixins.bloks import (
    AP_2SV_CODE_ENTRY,
    AP_2SV_CODE_ENTRY_ASYNC,
    AP_2SV_ENTRYPOINT,
)

from config import (
    INSTAGRAM_COUNTRY,
    INSTAGRAM_COUNTRY_CODE,
    INSTAGRAM_LOCALE,
    INSTAGRAM_PROXY,
    INSTAGRAM_USE_PROXY,
    INSTAGRAM_TIMEZONE_OFFSET,
    SESSION_ENCRYPTION_KEY,
    SESSIONS_DIR,
)

logger = logging.getLogger(__name__)

# Estado temporário das verificações em andamento. Nunca contém senha ou código.
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
    """Cliente Instagram com identidade estável e verificação não bloqueante."""

    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", self.username):
            raise ValueError("Nome de usuario do Instagram invalido.")
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{self.username}.json"
        self._fingerprint = device_fingerprint or {
            "device": "Pixel 8 Pro (perfil estável instagrapi)"
        }
        self._caa_send_result: dict | None = None
        self._caa_submit_context: dict | None = None
        self._two_step_context: str | None = None
        self._verification_mode: str | None = None
        self.cl = self._build_client()

    def _stable_bytes(self, label: str) -> bytes:
        key = str(SESSION_ENCRYPTION_KEY).encode("utf-8")
        data = f"instagram:{self.username.casefold()}:{label}".encode("utf-8")
        return hmac.new(key, data, hashlib.sha256).digest()

    def _stable_uuid(self, label: str) -> str:
        return str(uuid.UUID(bytes=self._stable_bytes(label)[:16], version=4))

    def _apply_network_identity(self, client: Client) -> None:
        # Impede que HTTP_PROXY/HTTPS_PROXY do servidor sejam usados por acidente.
        for session_name in ("private", "public"):
            session = getattr(client, session_name, None)
            if session is not None and hasattr(session, "trust_env"):
                session.trust_env = False
        if INSTAGRAM_USE_PROXY and INSTAGRAM_PROXY:
            client.set_proxy(INSTAGRAM_PROXY)
            logger.info("[%s] Proxy explícita configurada.", self.username)
        else:
            logger.info("[%s] Conexão direta, sem proxy.", self.username)
        client.set_country(INSTAGRAM_COUNTRY)
        client.set_country_code(INSTAGRAM_COUNTRY_CODE)
        client.set_locale(INSTAGRAM_LOCALE)
        client.set_timezone_offset(INSTAGRAM_TIMEZONE_OFFSET)

    def _build_client(self) -> Client:
        client = Client()
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
        return client

    def _reset_pending(self) -> None:
        PENDING_CHALLENGES.pop(self.username, None)
        self._caa_send_result = None
        self._caa_submit_context = None
        self._two_step_context = None
        self._verification_mode = None

    def _register_pending(self, mode: str, choice: str = "email_ou_app") -> None:
        self._verification_mode = mode
        PENDING_CHALLENGES[self.username] = {
            "client": self,
            "type": mode,
            "choice": choice,
        }

    def login(self) -> str:
        """Retorna imediatamente: ok, challenge, two_factor ou error:motivo."""
        if self.session_path.exists():
            try:
                self.cl.load_settings(str(self.session_path))
                self._apply_network_identity(self.cl)
                self.cl.get_timeline_feed()
                logger.info("[%s] Sessão restaurada com sucesso.", self.username)
                return "ok"
            except LoginRequired:
                logger.info("[%s] Sessão expirada; iniciando novo login.", self.username)
            except Exception as exc:
                logger.warning(
                    "[%s] Sessão salva não pôde ser reutilizada: %s",
                    self.username,
                    type(exc).__name__,
                )
            self.cl = self._build_client()
        return self._begin_caa_login()

    def _begin_caa_login(self) -> str:
        """Executa o CAA até a etapa anterior ao código e devolve o controle ao bot."""
        try:
            logger.info("[%s] Iniciando login CAA.", self.username)
            if not self.cl.bloks_caa_login_prepare(username=self.username):
                return "error:caa_preflight"

            result = self.cl.bloks_caa_login_send_request(
                self.password,
                username=self.username,
            )
            if self.cl.bloks_apply_login_response(result):
                self._save_session()
                self._reset_pending()
                return "ok"

            if self.cl.bloks_caa_login_needs_two_step(result):
                self._caa_send_result = result
                if not self._prepare_caa_challenge(result):
                    return "error:caa_challenge_prepare"
                self._register_pending("caa")
                return "challenge"

            context = self.cl.bloks_extract_two_step_verification_context(result)
            if context:
                self._two_step_context = context
                self._register_pending("2fa_bloks", "authenticator")
                return "two_factor"

            text = self.cl._bloks_all_text(result).casefold()
            if "incorrect password" in text or "senha incorreta" in text:
                # O CAA também usa "incorrect password" como recusa genérica
                # do fluxo/dispositivo. Tente o login padrão antes de concluir.
                logger.info(
                    "[%s] CAA recusou a credencial; tentando fluxo padrao.",
                    self.username,
                )
                return self._try_standard_login()
            logger.warning(
                "[%s] CAA sem sessao e sem contexto 2FA (challenge=%s, checkpoint=%s).",
                self.username,
                "challenge" in text,
                "checkpoint" in text,
            )
            return "error:caa_failed"
        except TwoFactorRequired:
            self._register_pending("2fa", "authenticator")
            return "two_factor"
        except BadPassword:
            return "error:credentials_rejected"
        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"
        except FeedbackRequired:
            return "error:feedback_required"
        except ReloginAttemptExceeded:
            return "error:relogin_exceeded"
        except Exception as exc:
            message = str(exc).lower()
            if "407" in message or "proxy" in message:
                return "error:proxy"
            if "429" in message or "too many" in message or "retry" in message:
                return "error:rate_limit_429"
            logger.error("[%s] Erro CAA: %s", self.username, type(exc).__name__)
            return f"error:{type(exc).__name__}"

    def _try_standard_login(self) -> str:
        """Tenta o fluxo padrão da biblioteca sem diagnosticar falsamente a senha."""
        try:
            if self.cl.login(self.username, self.password):
                self._save_session()
                self._reset_pending()
                return "ok"
            return "error:credentials_rejected"
        except TwoFactorRequired:
            self._register_pending("2fa", "authenticator")
            return "two_factor"
        except ChallengeRequired:
            return "error:challenge_required"
        except BadPassword:
            return "error:credentials_rejected"
        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"
        except FeedbackRequired:
            return "error:feedback_required"
        except Exception as exc:
            message = str(exc).lower()
            if "407" in message or "proxy" in message:
                return "error:proxy"
            if "429" in message or "too many" in message:
                return "error:rate_limit_429"
            logger.warning(
                "[%s] Fluxo padrao falhou: %s", self.username, type(exc).__name__
            )
            return f"error:{type(exc).__name__}"

    def _prepare_caa_challenge(self, send_result: dict) -> bool:
        entry_context = self.cl.bloks_extract_context_data(
            send_result, AP_2SV_ENTRYPOINT
        )
        if not entry_context:
            return False
        entry_result = self.cl.bloks_ap_two_step_verification_entrypoint(entry_context)
        code_context = self.cl.bloks_extract_context_data(
            entry_result, AP_2SV_CODE_ENTRY
        )
        if not code_context:
            return False
        code_result = self.cl.bloks_ap_two_step_verification_code_entry(code_context)
        self._caa_submit_context = self.cl.bloks_extract_context_data(
            code_result, AP_2SV_CODE_ENTRY_ASYNC
        )
        return bool(self._caa_submit_context)

    def _submit_caa_code(self, code: str) -> bool:
        clean = _normalize_code(code)
        if _detect_code_type(clean) == "unknown" or not self._caa_submit_context:
            return False
        try:
            result = self.cl.bloks_ap_two_step_verification_submit_code(
                self._caa_submit_context,
                clean,
            )
            if self.cl.bloks_apply_login_response(result):
                self._save_session()
                self._reset_pending()
                return True
        except Exception as exc:
            logger.warning(
                "[%s] Código CAA rejeitado: %s", self.username, type(exc).__name__
            )
        return False

    def start_challenge_with_method(self, method_type: str) -> str:
        # O fluxo CAA atual escolhe o canal no servidor.
        if self._caa_submit_context:
            return "challenge"
        return self._begin_caa_login()

    def submit_code(self, code: str) -> str:
        return "ok" if self._submit_caa_code(code) else "error"

    def submit_backup_code(self, code: str) -> bool:
        clean = _normalize_code(code)
        if len(clean) != 8:
            return False
        if self._caa_submit_context:
            return self._submit_caa_code(clean)
        return self.submit_2fa(clean)

    def submit_2fa(self, code: str) -> bool:
        clean = _normalize_code(code)
        if _detect_code_type(clean) == "unknown":
            return False
        if self._caa_submit_context:
            return self._submit_caa_code(clean)
        if self._two_step_context:
            return self._submit_bloks_two_factor(clean)
        try:
            if self.cl.login(self.username, self.password, verification_code=clean):
                self._save_session()
                self._reset_pending()
                return True
        except Exception as exc:
            logger.warning(
                "[%s] Código 2FA rejeitado: %s", self.username, type(exc).__name__
            )
        return False

    def _submit_bloks_two_factor(self, code: str) -> bool:
        context = self._two_step_context
        if not context:
            return False
        challenge = "backup_codes" if len(code) == 8 else "totp"
        try:
            self.cl.bloks_two_step_verification_entrypoint(context)
            self.cl.bloks_two_step_verification_method_picker(context)
            self.cl.bloks_two_step_verification_select_method(
                context, selected_method=challenge
            )
            if challenge == "backup_codes":
                self.cl.bloks_two_step_verification_enter_backup_code(context)
            result = self.cl.bloks_two_step_verification_verify_code(
                context, code, challenge=challenge
            )
            if self.cl.bloks_apply_login_response(result):
                self._save_session()
                self._reset_pending()
                return True
        except Exception as exc:
            logger.warning(
                "[%s] Codigo Bloks 2FA rejeitado: %s",
                self.username,
                type(exc).__name__,
            )
        return False

    def _save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.cl.dump_settings(str(self.session_path))

    def save_session(self) -> None:
        self._save_session()

    def get_session_data(self) -> dict:
        if self.session_path.exists():
            with self.session_path.open(encoding="utf-8") as session_file:
                return json.load(session_file)
        return {}

    def load_session_from_data(self, data: dict) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session_path.open("w", encoding="utf-8") as session_file:
            json.dump(data, session_file)
        self.cl.load_settings(str(self.session_path))
        self._apply_network_identity(self.cl)

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        return self.is_logged_in() or self.login() == "ok"

    def randomize_fingerprint(self):
        # Mantido por compatibilidade: a identidade deve permanecer estável.
        return self._fingerprint.copy()

    @property
    def api(self) -> Client:
        return self.cl


def detect_code_type(code: str) -> str:
    return _detect_code_type(code)


def format_preview(code: str) -> str:
    return _format_preview(code)
