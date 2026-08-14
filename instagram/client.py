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

# {username: {"client": InstagramClient, "code": None|str, "choice": str}}
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
        # Criar Client UMA vez — não recriar, preserva estado CAA
        self.cl = Client()
        # Substituir challenge_code_handler — intercepta pedido de código
        self.cl.challenge_code_handler = self._telegram_code_handler

    # ─── Handler que aguarda código do Telegram ───────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """
        O instagrapi chama isso automaticamente quando precisa de
        verificação (email/SMS/backup). Aguarda 5 min pelo Telegram.
        """
        from instagrapi.mixins.challenge import ChallengeChoice
        choice_label = {
            ChallengeChoice.EMAIL: "email",
            ChallengeChoice.SMS:   "sms",
        }.get(choice, "email_ou_sms")

        logger.info(f"[{username}] Instagram pediu verificacao via {choice_label}")
        logger.info(f"[{username}] Aguardando codigo no Telegram...")

        PENDING_CHALLENGES[username] = {
            "client": self,
            "code": None,
            "choice": choice_label,
        }

        elapsed = 0
        while elapsed < 300:
            entry = PENDING_CHALLENGES.get(username)
            if entry and entry.get("code"):
                code = _normalize_code(entry["code"])
                PENDING_CHALLENGES.pop(username, None)
                logger.info(f"[{username}] Codigo recebido e injetado: {code}")
                return code
            time.sleep(2)
            elapsed += 2

        PENDING_CHALLENGES.pop(username, None)
        logger.error(f"[{username}] Timeout — codigo nao fornecido em 5 minutos.")
        return ""

    # ─── Login ────────────────────────────────────────────────

    def login(self) -> str:
        """
        Usa cl.login() do instagrapi que gerencia todo o fluxo:
        legacy login → CAA fallback → challenge_code_handler (nosso).
        Só capturamos BadPassword real e erros irrecuperáveis.
        """
        # Restaurar sessão salva
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessao...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessao restaurada com sucesso.")
                return "ok"
            except LoginRequired:
                logger.warning(f"[{self.username}] Sessao expirada.")
            except Exception as e:
                logger.warning(f"[{self.username}] Erro ao restaurar: {e}")

        return self._do_login()

    def _do_login(self, verification_code: str = "") -> str:
        try:
            logger.info(f"[{self.username}] Iniciando login...")
            # login() gerencia: pre_login → accounts/login/ → CAA fallback
            # → challenge_code_handler quando precisar de verificação
            self.cl.login(self.username, self.password,
                          verification_code=verification_code)
            self._save_session()
            logger.info(f"[{self.username}] Login concluido com sucesso.")
            return "ok"

        except TwoFactorRequired:
            # Instagram pediu 2FA — usuário precisa fornecer código
            logger.info(f"[{self.username}] 2FA requerido.")
            PENDING_CHALLENGES[self.username] = {
                "client": self, "code": None, "type": "2fa"
            }
            return "two_factor"

        except ChallengeRequired:
            # challenge_code_handler foi chamado e está aguardando
            # (já registrou em PENDING_CHALLENGES)
            logger.info(f"[{self.username}] Challenge — aguardando codigo.")
            return "challenge"

        except BadPassword as e:
            # BadPassword REAL — senha errada
            # (se fosse CAA, _try_caa_login teria resolvido internamente)
            logger.error(f"[{self.username}] Senha incorreta: {e}")
            return "error:bad_password"

        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"

        except FeedbackRequired:
            return "error:feedback_required"

        except ReloginAttemptExceeded:
            return "error:relogin_exceeded"

        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many" in err or "retry" in err:
                return "error:rate_limit_429"
            logger.error(f"[{self.username}] Erro: {type(e).__name__}: {e}")
            return f"error:{type(e).__name__}: {e}"

    def start_challenge_with_method(self, method_type: str) -> str:
        """Reinicia o login — challenge_code_handler aguardará o código."""
        return self._do_login()

    # ─── Submissão de código ──────────────────────────────────

    def submit_code(self, code: str) -> str:
        """Injeta código no challenge_code_handler que está aguardando."""
        clean = _normalize_code(code)
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            return "pending"
        return "error"

    def submit_backup_code(self, code: str) -> bool:
        """Submete backup code de 8 dígitos."""
        clean = _normalize_code(code)
        logger.info(f"[{self.username}] Submetendo backup code: {clean}")

        # Se challenge_code_handler está aguardando, injetar
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = clean
            for _ in range(8):
                time.sleep(2)
                if self.username not in PENDING_CHALLENGES:
                    return self.is_logged_in()
            return self.is_logged_in()

        # Tentar login direto com código de backup
        try:
            self.cl.login(self.username, self.password, verification_code=clean)
            self._save_session()
            return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro backup code: {e}")
        return False

    def submit_2fa(self, code: str) -> bool:
        """Submete código de 2FA."""
        clean = _normalize_code(code)
        try:
            self.cl.login(self.username, self.password, verification_code=clean)
            self._save_session()
            PENDING_CHALLENGES.pop(self.username, None)
            return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro 2FA: {e}")
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

    def randomize_fingerprint(self):
        self.cl.set_settings({
            "device_id": f"android-{uuid.uuid4().hex[:16]}",
            "uuid": str(uuid.uuid4()),
            "phone_id": str(uuid.uuid4()),
        })
        return self._fingerprint

    @property
    def api(self) -> Client:
        return self.cl


def detect_code_type(code: str) -> str:
    return _detect_code_type(code)

def format_preview(code: str) -> str:
    return _format_preview(code)
