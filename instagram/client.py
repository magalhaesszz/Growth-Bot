import json
import logging
import re
import time
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

from config import SESSIONS_DIR

logger = logging.getLogger(__name__)

# {username: {"client": InstagramClient, "code": None|str, "type": str}}
PENDING_CHALLENGES: dict[str, dict] = {}


def _normalize_code(code: str) -> str:
    """Remove espaços, hífens e traços."""
    return re.sub(r"[\s\-]+", "", str(code).strip())


def _detect_code_type(code: str) -> str:
    """
    Detecta o tipo de código automaticamente:
    - 6 dígitos → SMS ou email
    - 8 dígitos → backup code
    - 6 dígitos TOTP → autenticador
    """
    clean = _normalize_code(code)
    if re.fullmatch(r"\d{8}", clean):
        return "backup"
    if re.fullmatch(r"\d{6}", clean):
        return "sms_or_totp"
    return "unknown"


def _format_preview(code: str) -> str:
    """Formata o código para preview antes de enviar."""
    clean = _normalize_code(code)
    tipo = _detect_code_type(code)
    if tipo == "backup":
        # Formatar como XXXX-XXXX
        return f"{clean[:4]}-{clean[4:]}"
    if tipo == "sms_or_totp":
        # Formatar como XXX-XXX
        return f"{clean[:3]}-{clean[3:]}"
    return clean


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self._fingerprint = {"device": "Pixel 8 Pro (padrao instagrapi)"}
        self.cl = self._build_client()

    def _build_client(self) -> Client:
        cl = Client()
        cl.set_settings({
            "device_id": f"android-{uuid.uuid4().hex[:16]}",
            "uuid": str(uuid.uuid4()),
            "phone_id": str(uuid.uuid4()),
            "client_session_id": str(uuid.uuid4()),
        })
        cl.challenge_code_handler = self._telegram_code_handler
        return cl

    def randomize_fingerprint(self):
        self.cl = self._build_client()
        logger.info(f"[{self.username}] UUIDs randomizados.")
        return self._fingerprint

    # ─── Challenge handler via Telegram ──────────────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """Aguarda código digitado no Telegram por até 5 minutos."""
        logger.info(f"[{username}] Aguardando codigo via Telegram...")
        PENDING_CHALLENGES[username] = {"client": self, "code": None}
        elapsed = 0
        while elapsed < 300:
            entry = PENDING_CHALLENGES.get(username)
            if entry and entry.get("code"):
                code = _normalize_code(entry["code"])
                PENDING_CHALLENGES.pop(username, None)
                logger.info(f"[{username}] Codigo recebido: {code}")
                return code
            time.sleep(2)
            elapsed += 2
        PENDING_CHALLENGES.pop(username, None)
        logger.error(f"[{username}] Timeout aguardando codigo.")
        return ""

    # ─── Login ───────────────────────────────────────────────

    def login(self) -> str:
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessao...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessao restaurada.")
                return "ok"
            except LoginRequired:
                self.cl = self._build_client()
            except ChallengeRequired:
                return self._handle_challenge_flow()
            except Exception as e:
                logger.warning(f"[{self.username}] Erro na restauracao: {e}")
                self.cl = self._build_client()
        return self._full_login()

    def _full_login(self) -> str:
        try:
            logger.info(f"[{self.username}] Login completo...")
            self.cl.login(self.username, self.password)
            self._save_session()
            return "ok"
        except ChallengeRequired:
            return self._handle_challenge_flow()
        except TwoFactorRequired:
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "type": "2fa"}
            return "two_factor"
        except BadPassword as e:
            err = str(e).lower()
            last = self.cl.last_json or {}
            last_str = json.dumps(last).lower()
            if any(x in err for x in ["email", "send you", "get back", "upgrade", "verify"]):
                return self._handle_challenge_flow()
            if last.get("two_factor_info") or "two_factor" in last_str:
                PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "type": "2fa"}
                return "two_factor"
            if last.get("challenge") or "challenge" in last_str:
                return self._handle_challenge_flow()
            logger.error(f"[{self.username}] Senha incorreta: {e}")
            return "error:bad_password"
        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many" in err or "retry" in err:
                return "error:rate_limit_429"
            logger.error(f"[{self.username}] Erro: {type(e).__name__}: {e}")
            return f"error:{type(e).__name__}: {e}"

    def _handle_challenge_flow(self) -> str:
        try:
            logger.info(f"[{self.username}] Iniciando challenge_flow...")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            result = self.cl.challenge_flow(self.cl.last_json)
            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return "ok"
            return "challenge"
        except Exception as e:
            logger.error(f"[{self.username}] Erro no challenge_flow: {e}")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

    def start_challenge_with_method(self, method_type: str) -> str:
        try:
            last = self.cl.last_json or {}
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "choice": method_type}
            result = self.cl.challenge_flow(last)
            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return "ok"
            return "challenge"
        except Exception as e:
            logger.error(f"[{self.username}] Erro no challenge com metodo {method_type}: {e}")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

    # ─── Submissão de códigos ─────────────────────────────────

    def submit_code(self, code: str) -> str:
        """
        Detecta automaticamente o tipo e submete o código.
        Retorna: 'ok' | 'pending' | 'error'
        """
        clean = _normalize_code(code)
        tipo = _detect_code_type(clean)
        logger.info(f"[{self.username}] Submetendo codigo tipo={tipo}: {clean}")

        # Injetar no handler que está aguardando (challenge_flow em outra thread)
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            return "pending"  # aguardar challenge_flow completar

        # Se não há challenge ativo, tentar login direto com código
        try:
            result = self.cl.login(
                self.username, self.password,
                verification_code=clean
            )
            if result:
                self._save_session()
                return "ok"
        except Exception as e:
            logger.error(f"[{self.username}] Erro ao submeter codigo: {e}")

        return "error"

    def submit_backup_code(self, code: str) -> bool:
        """Submete backup code de 8 dígitos."""
        clean = _normalize_code(code)
        logger.info(f"[{self.username}] Submetendo backup code: {clean}")

        # Primeiro tentar via challenge_code_handler se há challenge ativo
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            # Aguardar até 10s para o challenge_flow processar
            for _ in range(5):
                time.sleep(2)
                if self.username not in PENDING_CHALLENGES:
                    return self.is_logged_in()
            return self.is_logged_in()

        # Tentar login direto com backup code
        try:
            result = self.cl.login(
                self.username, self.password,
                verification_code=clean
            )
            if result:
                self._save_session()
                return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no backup code: {e}")

        # Tentar via bloks se disponível
        try:
            last = self.cl.last_json or {}
            context = self.cl._extract_two_step_verification_context(last)
            if context:
                res = self.cl.bloks_two_step_verification_enter_backup_code(context)
                if res:
                    # Submeter o código no próximo passo
                    time.sleep(1)
                    result2 = self.cl.login(self.username, self.password,
                                            verification_code=clean)
                    if result2:
                        self._save_session()
                        return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no backup code via bloks: {e}")

        return False

    def submit_2fa(self, code: str) -> bool:
        clean = _normalize_code(code)
        try:
            result = self.cl.login(self.username, self.password,
                                   verification_code=clean)
            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no 2FA: {e}")
        return False

    # ─── Sessão ──────────────────────────────────────────────

    def _save_session(self):
        self.cl.dump_settings(str(self.session_path))

    def save_session(self):
        self._save_session()

    def get_session_data(self) -> dict:
        if self.session_path.exists():
            with open(self.session_path) as f:
                return json.load(f)
        return {}

    def load_session_from_data(self, data: dict):
        with open(str(self.session_path), "w") as f:
            json.dump(data, f)
        self.cl.load_settings(str(self.session_path))

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        if not self.is_logged_in():
            return self.login() == "ok"
        return True

    @property
    def api(self) -> Client:
        return self.cl


# Exportar funções utilitárias
def detect_code_type(code: str) -> str:
    return _detect_code_type(code)

def format_preview(code: str) -> str:
    return _format_preview(code)
