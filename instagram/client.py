import json
import logging
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

# Clientes aguardando código: {username: {"client": InstagramClient, "code": None|str, "type": str}}
PENDING_CHALLENGES: dict[str, dict] = {}


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        # device_fingerprint ignorado — usamos o padrão do instagrapi
        # que sempre tem a versão correta do app
        self._fingerprint = {"device": "Pixel 8 Pro (padrão instagrapi)"}
        self.cl = self._build_client()

    def _build_client(self) -> Client:
        """Cria um Client com o dispositivo padrão do instagrapi (versão sempre atualizada)."""
        cl = Client()
        # NÃO chamamos set_device() — o padrão já está correto e atualizado
        # Apenas randomizamos o device_id para anti-ban
        cl.set_settings({
            "device_id": f"android-{uuid.uuid4().hex[:16]}",
            "uuid": str(uuid.uuid4()),
            "phone_id": str(uuid.uuid4()),
            "client_session_id": str(uuid.uuid4()),
        })
        cl.challenge_code_handler = self._telegram_code_handler
        return cl

    def randomize_fingerprint(self):
        """Regenera os UUIDs do dispositivo para anti-ban."""
        self.cl = self._build_client()
        logger.info(f"[{self.username}] UUIDs de dispositivo randomizados.")
        return self._fingerprint

    # ─── Challenge handler via Telegram ──────────────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """
        Substitui o input() padrão do instagrapi.
        Aguarda até 5 minutos pelo código digitado no Telegram.
        """
        logger.info(f"[{username}] Aguardando código via Telegram (choice={choice})...")
        PENDING_CHALLENGES[username] = {"client": self, "code": None}
        timeout = 300
        elapsed = 0
        while elapsed < timeout:
            entry = PENDING_CHALLENGES.get(username)
            if entry and entry.get("code"):
                code = entry["code"]
                PENDING_CHALLENGES.pop(username, None)
                logger.info(f"[{username}] Código recebido: {code}")
                return str(code)
            time.sleep(2)
            elapsed += 2
        PENDING_CHALLENGES.pop(username, None)
        logger.error(f"[{username}] Timeout aguardando código.")
        return ""

    # ─── Login ───────────────────────────────────────────────

    def login(self) -> str:
        """Retorna: 'ok' | 'challenge' | 'two_factor' | 'error:motivo'"""

        # Tentar restaurar sessão salva
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessão...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessão restaurada.")
                return "ok"
            except LoginRequired:
                logger.warning(f"[{self.username}] Sessão expirada.")
                self.cl = self._build_client()
            except ChallengeRequired:
                return self._handle_challenge_flow()
            except Exception as e:
                logger.warning(f"[{self.username}] Erro na restauração: {e}")
                self.cl = self._build_client()

        return self._full_login()

    def _full_login(self) -> str:
        try:
            logger.info(f"[{self.username}] Login completo...")
            self.cl.login(self.username, self.password)
            self._save_session()
            logger.info(f"[{self.username}] Login com sucesso.")
            return "ok"

        except ChallengeRequired:
            return self._handle_challenge_flow()

        except TwoFactorRequired:
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "type": "2fa"}
            return "two_factor"

        except BadPassword as e:
            err_str = str(e).lower()
            last    = self.cl.last_json or {}
            last_str = json.dumps(last).lower()

            # Instagram pedindo verificação via email/SMS (CAA login)
            if any(x in err_str for x in ["email", "send you", "get back", "upgrade"]):
                logger.warning(f"[{self.username}] BadPassword com contexto de verificação: {e}")
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
        except FeedbackRequired:
            return "error:feedback_required"
        except ReloginAttemptExceeded:
            return "error:relogin_exceeded"
        except Exception as e:
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
                logger.info(f"[{self.username}] Challenge resolvido.")
                return "ok"
            logger.warning(f"[{self.username}] challenge_flow retornou False.")
            return "challenge"
        except Exception as e:
            logger.error(f"[{self.username}] Erro no challenge_flow: {e}")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

    def submit_code(self, code: str) -> bool:
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = str(code).strip()
            return True
        return False

    def submit_2fa(self, code: str) -> bool:
        try:
            result = self.cl.login(self.username, self.password,
                                   verification_code=str(code).strip())
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
