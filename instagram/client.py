import os
import json
import random
import logging
from pathlib import Path
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    PleaseWaitFewMinutes,
    ChallengeRequired,
    BadPassword,
    InvalidTargetUser,
    UserNotFound,
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

# Estado global de challenges pendentes: {username: InstagramClient}
PENDING_CHALLENGES: dict[str, "InstagramClient"] = {}


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self.cl = Client()
        self._fingerprint = device_fingerprint or random.choice(DEVICE_POOL)
        self._challenge_pending = False
        self._apply_fingerprint()

    # ─── Fingerprint ─────────────────────────────────────────

    def _apply_fingerprint(self):
        self.cl.set_device(self._fingerprint)
        self.cl.set_user_agent()

    def randomize_fingerprint(self):
        self._fingerprint = random.choice(DEVICE_POOL)
        self._apply_fingerprint()
        logger.info(f"[{self.username}] Fingerprint atualizado: {self._fingerprint['device']}")
        return self._fingerprint

    # ─── Login principal ─────────────────────────────────────

    def login(self) -> str:
        """
        Tenta login. Retorna:
          'ok'        — logado com sucesso
          'challenge' — Instagram pediu verificação por código
          'error'     — falha definitiva
        """
        # 1. Tentar restaurar sessão salva
        if self.session_path.exists():
            try:
                logger.info(f"[{self.username}] Restaurando sessão salva...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()
                logger.info(f"[{self.username}] Sessão restaurada com sucesso.")
                return "ok"
            except LoginRequired:
                logger.warning(f"[{self.username}] Sessão expirada, fazendo login completo...")
            except ChallengeRequired:
                return self._handle_challenge()
            except Exception as e:
                logger.warning(f"[{self.username}] Erro ao restaurar sessão: {e}")

        # 2. Login completo com credenciais
        return self._full_login()

    def _full_login(self) -> str:
        try:
            self.cl.login(self.username, self.password)
            self._save_session()
            logger.info(f"[{self.username}] Login realizado com sucesso.")
            return "ok"

        except ChallengeRequired:
            return self._handle_challenge()

        except BadPassword:
            logger.error(f"[{self.username}] Senha incorreta.")
            return "error:bad_password"

        except InvalidTargetUser:
            logger.error(f"[{self.username}] Usuário não encontrado.")
            return "error:invalid_user"

        except PleaseWaitFewMinutes:
            logger.error(f"[{self.username}] Instagram pedindo espera. Tente em alguns minutos.")
            return "error:rate_limit"

        except Exception as e:
            logger.error(f"[{self.username}] Falha no login: {e}")
            return f"error:{e}"

    # ─── Challenge (verificação por código) ──────────────────

    def _handle_challenge(self) -> str:
        """Solicita o envio do código de verificação para email/SMS."""
        try:
            logger.info(f"[{self.username}] Challenge detectado. Solicitando código...")
            # Solicita ao Instagram enviar o código via SMS ou email
            self.cl.challenge_resolve(self.cl.last_json)
            self._challenge_pending = True
            PENDING_CHALLENGES[self.username] = self
            logger.info(f"[{self.username}] Código solicitado. Aguardando entrada do usuário.")
            return "challenge"
        except Exception as e:
            logger.error(f"[{self.username}] Erro ao resolver challenge: {e}")
            # Fallback: mesmo sem resolver automaticamente, marca como pendente
            self._challenge_pending = True
            PENDING_CHALLENGES[self.username] = self
            return "challenge"

    def submit_challenge_code(self, code: str) -> bool:
        """Submete o código de verificação recebido por SMS/email."""
        try:
            # Tenta método direto do instagrapi
            result = self.cl.challenge_resolve(self.cl.last_json, security_code=code)
            if result:
                self._challenge_pending = False
                PENDING_CHALLENGES.pop(self.username, None)
                self._save_session()
                logger.info(f"[{self.username}] Challenge resolvido com sucesso!")
                return True
        except Exception:
            pass

        # Fallback: submete manualmente via endpoint de challenge
        try:
            challenge_url = self.cl.last_json.get("challenge", {}).get("url", "")
            if challenge_url:
                self.cl.private.post(
                    challenge_url,
                    data={"security_code": code},
                )
                self.cl.login(self.username, self.password)
                self._challenge_pending = False
                PENDING_CHALLENGES.pop(self.username, None)
                self._save_session()
                logger.info(f"[{self.username}] Challenge resolvido via fallback.")
                return True
        except Exception as e:
            logger.error(f"[{self.username}] Falha ao submeter código: {e}")

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
        settings_path = str(self.session_path)
        with open(settings_path, "w") as f:
            json.dump(data, f)
        self.cl.load_settings(settings_path)

    # ─── Saúde da sessão ─────────────────────────────────────

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        if not self.is_logged_in():
            logger.warning(f"[{self.username}] Sessão inativa. Reconectando...")
            result = self.login()
            return result == "ok"
        return True

    @property
    def api(self) -> Client:
        return self.cl
