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

# {username: {"client": InstagramClient, "code": None|str}}
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
    tipo = _detect_code_type(clean)
    if tipo == "backup":
        return f"{clean[:4]}-{clean[4:]}"
    if tipo == "sms_or_totp":
        return f"{clean[:3]}-{clean[3:]}"
    return clean


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self._fingerprint = {"device": "Pixel 8 Pro (padrao instagrapi)"}
        self._pending_code: str | None = None
        self.cl = self._build_client()

    def _build_client(self) -> Client:
        cl = Client()
        cl.set_settings({
            "device_id": f"android-{uuid.uuid4().hex[:16]}",
            "uuid": str(uuid.uuid4()),
            "phone_id": str(uuid.uuid4()),
            "client_session_id": str(uuid.uuid4()),
        })
        # Substituir challenge_code_handler pelo nosso que aguarda Telegram
        cl.challenge_code_handler = self._telegram_code_handler
        return cl

    def randomize_fingerprint(self):
        self.cl = self._build_client()
        return self._fingerprint

    # ─── Handler de código via Telegram ──────────────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """
        Chamado pelo instagrapi (challenge_code_or_raised) quando precisa
        de verificação. Aguarda até 5 min pelo código digitado no Telegram.
        """
        from instagrapi.mixins.challenge import ChallengeChoice
        choice_label = {
            ChallengeChoice.EMAIL: "email",
            ChallengeChoice.SMS: "sms",
        }.get(choice, "email_ou_sms")

        logger.info(f"[{username}] Aguardando codigo via Telegram (metodo: {choice_label})...")
        PENDING_CHALLENGES[username] = {"client": self, "code": None, "choice": choice_label}

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

    # ─── Login principal ─────────────────────────────────────

    def login(self) -> str:
        """Retorna: 'ok' | 'challenge' | 'two_factor' | 'error:motivo'"""

        # Tentar sessão salva primeiro
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessao...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessao restaurada.")
                return "ok"
            except LoginRequired:
                logger.warning(f"[{self.username}] Sessao expirada.")
                self.cl = self._build_client()
            except (ChallengeRequired, TwoFactorRequired):
                pass
            except Exception as e:
                logger.warning(f"[{self.username}] Erro na restauracao: {e}")
                self.cl = self._build_client()

        return self._caa_login()

    def _caa_login(self, verification_code: str = "") -> str:
        """
        Usa bloks_caa_login — o fluxo correto para o Instagram atual.
        Se precisar de verificação, o challenge_code_handler é chamado
        automaticamente pelo instagrapi e aguarda o código do Telegram.
        """
        try:
            logger.info(f"[{self.username}] Iniciando CAA login...")

            # Marcar como aguardando para o Telegram saber que pode
            # receber o código a qualquer momento
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}

            outcome = self.cl.bloks_caa_login(
                username=self.username,
                password=self.password,
                verification_code=verification_code,
            )

            if outcome.get("logged_in"):
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                logger.info(f"[{self.username}] Login CAA com sucesso.")
                return "ok"

            reason = outcome.get("reason", "")
            logger.error(f"[{self.username}] CAA login falhou: {reason}")
            return f"error:{reason}" if reason else "error:caa_failed"

        except TwoFactorRequired:
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "type": "2fa"}
            return "two_factor"

        except ChallengeRequired as e:
            logger.warning(f"[{self.username}] ChallengeRequired no CAA: {e}")
            # challenge_code_handler já foi chamado e está aguardando
            # verificar se o código foi recebido
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"

        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many" in err or "retry" in err:
                return "error:rate_limit_429"
            logger.error(f"[{self.username}] Erro CAA: {type(e).__name__}: {e}")
            return f"error:{type(e).__name__}: {e}"

    def start_challenge_with_method(self, method_type: str) -> str:
        """Inicia CAA login com método específico — o handler aguarda o código."""
        return self._caa_login()

    # ─── Submissão de código quando o bot já está aguardando ─

    def submit_code(self, code: str) -> str:
        """Injeta código no handler que está aguardando. Retorna 'pending' ou 'error'."""
        clean = _normalize_code(code)
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            return "pending"
        return "error"

    def submit_backup_code(self, code: str) -> bool:
        """Submete backup code de 8 dígitos."""
        clean = _normalize_code(code)
        logger.info(f"[{self.username}] Tentando backup code: {clean}")

        # Se há challenge ativo, injetar o código
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            # Aguardar o challenge_flow processar
            for _ in range(8):
                time.sleep(2)
                if self.username not in PENDING_CHALLENGES:
                    return self.is_logged_in()
            return self.is_logged_in()

        # Sem challenge ativo — tentar login direto com código
        try:
            outcome = self.cl.bloks_caa_login(
                username=self.username,
                password=self.password,
                verification_code=clean,
            )
            if outcome.get("logged_in"):
                self._save_session()
                return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no backup code via CAA: {e}")

        return False

    def submit_2fa(self, code: str) -> bool:
        clean = _normalize_code(code)
        try:
            outcome = self.cl.bloks_caa_login(
                username=self.username,
                password=self.password,
                verification_code=clean,
            )
            if outcome.get("logged_in"):
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


def detect_code_type(code: str) -> str:
    return _detect_code_type(code)

def format_preview(code: str) -> str:
    return _format_preview(code)
