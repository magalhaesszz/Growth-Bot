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
    TwoFactorRequired,
)

from config import SESSIONS_DIR

logger = logging.getLogger(__name__)

BLOKS_VERSIONING_ID = "7189b949425f9bf80ea8bd880cf5a3080b292d9b1c4b38a18d112f7c4b71e7a8"

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
        "bloks_versioning_id": BLOKS_VERSIONING_ID,
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
        "bloks_versioning_id": BLOKS_VERSIONING_ID,
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
        "bloks_versioning_id": BLOKS_VERSIONING_ID,
    },
]

# Clientes aguardando código: {username: {"client": InstagramClient, "code": None|str}}
PENDING_CHALLENGES: dict[str, dict] = {}


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self._fingerprint = device_fingerprint or random.choice(DEVICE_POOL)
        self.cl = self._build_client()

    def _build_client(self) -> Client:
        cl = Client()
        # Garantir que bloks_versioning_id está presente antes de set_device
        fp = dict(self._fingerprint)
        fp.setdefault("bloks_versioning_id", BLOKS_VERSIONING_ID)
        cl.set_device(fp)
        cl.set_user_agent()
        cl.challenge_code_handler = self._telegram_code_handler
        return cl

    # ─── Fingerprint ─────────────────────────────────────────

    def randomize_fingerprint(self):
        self._fingerprint = random.choice(DEVICE_POOL)
        self.cl = self._build_client()
        logger.info(f"[{self.username}] Fingerprint: {self._fingerprint['device']}")
        return self._fingerprint

    # ─── Challenge handler via Telegram ──────────────────────

    def _telegram_code_handler(self, username: str, choice=None) -> str:
        logger.info(f"[{username}] Aguardando código de verificação via Telegram...")
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
        except BadPassword:
            return "error:bad_password"
        except (PleaseWaitFewMinutes, RateLimitError):
            return "error:rate_limit"
        except FeedbackRequired:
            return "error:feedback_required"
        except ReloginAttemptExceeded:
            return "error:relogin_exceeded"
        except Exception as e:
            logger.error(f"[{self.username}] Erro no login: {e}")
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
