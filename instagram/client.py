import asyncio
import json
import logging
import random
import time
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
    SelectContactPointRecoveryForm,
    TwoFactorRequired,
)

from config import SESSIONS_DIR

logger = logging.getLogger(__name__)

DEVICE_POOL = [
    {
        "app_version": "269.0.0.18.75",
        "android_version": 26,
        "android_release": "8.0.0",
        "dpi": "480dpi",
        "resolution": "1080x1920",
        "manufacturer": "Samsung",
        "device": "SM-G955F",
        "model": "dream2qltesq",
        "cpu": "samsungexynos8895",
        "version_code": "314665256",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 28,
        "android_release": "9.0.0",
        "dpi": "420dpi",
        "resolution": "1080x2220",
        "manufacturer": "Xiaomi",
        "device": "Redmi Note 7",
        "model": "lavender",
        "cpu": "qcom",
        "version_code": "314665256",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 29,
        "android_release": "10.0",
        "dpi": "440dpi",
        "resolution": "1080x2340",
        "manufacturer": "Motorola",
        "device": "moto g7 power",
        "model": "ocean",
        "cpu": "qcom",
        "version_code": "314665256",
    },
]

# Armazena clientes aguardando código de verificação
# {username: {"client": InstagramClient, "code": None | str}}
PENDING_CHALLENGES: dict[str, dict] = {}


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self.cl = Client()
        self._fingerprint = device_fingerprint or random.choice(DEVICE_POOL)
        self._apply_fingerprint()

        # Substituir o challenge_code_handler padrão (que usa input())
        # pelo nosso handler que espera o código via Telegram
        self.cl.challenge_code_handler = self._telegram_code_handler

    # ─── Fingerprint ─────────────────────────────────────────

    def _apply_fingerprint(self):
        self.cl.set_device(self._fingerprint)
        self.cl.set_user_agent()

    def randomize_fingerprint(self):
        self._fingerprint = random.choice(DEVICE_POOL)
        self._apply_fingerprint()
        logger.info(f"[{self.username}] Fingerprint: {self._fingerprint['device']}")
        return self._fingerprint

    # ─── Challenge handler via Telegram ──────────────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        """
        Substitui o input() padrão do instagrapi.
        Registra o username como pendente e aguarda até 5 minutos
        pelo código que o usuário vai digitar no Telegram.
        """
        logger.info(f"[{username}] Aguardando código de verificação via Telegram...")
        PENDING_CHALLENGES[username] = {"client": self, "code": None}

        # Aguarda o código por até 5 minutos
        timeout = 300
        interval = 2
        elapsed = 0
        while elapsed < timeout:
            entry = PENDING_CHALLENGES.get(username)
            if entry and entry.get("code"):
                code = entry["code"]
                PENDING_CHALLENGES.pop(username, None)
                logger.info(f"[{username}] Código recebido via Telegram: {code}")
                return str(code)
            time.sleep(interval)
            elapsed += interval

        PENDING_CHALLENGES.pop(username, None)
        logger.error(f"[{username}] Timeout aguardando código de verificação.")
        return ""

    # ─── Login principal ──────────────────────────────────────

    def login(self) -> str:
        """
        Retorna: 'ok' | 'challenge' | 'two_factor' | 'error:motivo'
        """
        # Tentar restaurar sessão salva
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessão...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessão restaurada com sucesso.")
                return "ok"
            except LoginRequired:
                logger.warning(f"[{self.username}] Sessão expirada. Login completo...")
                self.cl = Client()
                self._apply_fingerprint()
                self.cl.challenge_code_handler = self._telegram_code_handler
            except ChallengeRequired:
                logger.warning(f"[{self.username}] Challenge na restauração.")
                return self._handle_challenge_flow()
            except Exception as e:
                logger.warning(f"[{self.username}] Erro na restauração: {e}")
                self.cl = Client()
                self._apply_fingerprint()
                self.cl.challenge_code_handler = self._telegram_code_handler

        return self._full_login()

    def _full_login(self) -> str:
        try:
            logger.info(f"[{self.username}] Fazendo login completo...")
            self.cl.login(self.username, self.password)
            self._save_session()
            logger.info(f"[{self.username}] Login com sucesso.")
            return "ok"

        except ChallengeRequired:
            logger.warning(f"[{self.username}] Challenge requerido.")
            return self._handle_challenge_flow()

        except TwoFactorRequired:
            logger.warning(f"[{self.username}] 2FA requerido.")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "type": "2fa"}
            return "two_factor"

        except BadPassword:
            return "error:bad_password"

        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"

        except FeedbackRequired:
            return "error:feedback_required"

        except ReloginAttemptExceeded:
            return "error:relogin_exceeded"

        except SelectContactPointRecoveryForm:
            return "error:contact_point_recovery"

        except Exception as e:
            logger.error(f"[{self.username}] Erro no login: {e}")
            return f"error:{type(e).__name__}: {e}"

    def _handle_challenge_flow(self) -> str:
        """
        Usa o challenge_flow do instagrapi que internamente chama
        o challenge_code_handler (que substituímos pelo _telegram_code_handler).
        O _telegram_code_handler bloqueia aguardando o código via Telegram.
        """
        try:
            logger.info(f"[{self.username}] Iniciando fluxo de challenge...")
            # Marca como pendente para que o handler do Telegram saiba
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}

            # challenge_flow já chama challenge_code_handler internamente
            result = self.cl.challenge_flow(self.cl.last_json)

            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                logger.info(f"[{self.username}] Challenge resolvido com sucesso.")
                return "ok"
            else:
                logger.error(f"[{self.username}] challenge_flow retornou False.")
                return "challenge"

        except Exception as e:
            logger.error(f"[{self.username}] Erro no challenge_flow: {e}")
            # Mesmo com erro, marca como pendente para o usuário tentar o código
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

    def submit_code(self, code: str) -> bool:
        """
        Recebe o código digitado no Telegram e o injeta no handler.
        O _telegram_code_handler está aguardando em outro thread.
        """
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = str(code).strip()
            logger.info(f"[{self.username}] Código injetado: {code}")
            return True

        # Fallback: tenta submeter diretamente via challenge_resolve_simple
        try:
            challenge_url = self.cl.last_json.get("challenge", {}).get("url", "")
            if challenge_url:
                result = self.cl.challenge_resolve_simple(challenge_url)
                if result:
                    self._save_session()
                    return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no submit direto: {e}")

        return False

    def submit_2fa(self, code: str) -> bool:
        """Submete código de 2FA."""
        try:
            result = self.cl.login(
                self.username, self.password,
                verification_code=str(code).strip()
            )
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
        logger.debug(f"[{self.username}] Sessão salva.")

    def save_session(self):
        self._save_session()

    def get_session_data(self) -> dict:
        if self.session_path.exists():
            with open(self.session_path, "r") as f:
                return json.load(f)
        return {}

    def load_session_from_data(self, data: dict):
        with open(str(self.session_path), "w") as f:
            json.dump(data, f)
        self.cl.load_settings(str(self.session_path))

    # ─── Saúde ───────────────────────────────────────────────

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        if not self.is_logged_in():
            result = self.login()
            return result == "ok"
        return True

    @property
    def api(self) -> Client:
        return self.cl
