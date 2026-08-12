import os
import json
import random
import logging
from pathlib import Path
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, PleaseWaitFewMinutes, ChallengeRequired

from config import SESSIONS_DIR

logger = logging.getLogger(__name__)

# ─── Fingerprints realistas de dispositivos Android ──────────
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


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self.cl = Client()
        self._fingerprint = device_fingerprint or random.choice(DEVICE_POOL)
        self._apply_fingerprint()

    # ─── Fingerprint ─────────────────────────────────────────

    def _apply_fingerprint(self):
        self.cl.set_device(self._fingerprint)
        self.cl.set_user_agent()

    def randomize_fingerprint(self):
        """Troca fingerprint (chamar periodicamente ou após suspeita)."""
        self._fingerprint = random.choice(DEVICE_POOL)
        self._apply_fingerprint()
        logger.info(f"[{self.username}] Fingerprint atualizado: {self._fingerprint['device']}")
        return self._fingerprint

    # ─── Login / sessão ──────────────────────────────────────

    def login(self) -> bool:
        """Login com sessão salva ou credenciais. Retorna True se ok."""
        try:
            if self.session_path.exists():
                logger.info(f"[{self.username}] Restaurando sessão salva...")
                self.cl.load_settings(str(self.session_path))
                self.cl.login(self.username, self.password)
                self.cl.get_timeline_feed()  # valida sessão
                logger.info(f"[{self.username}] Sessão restaurada com sucesso.")
                return True
        except LoginRequired:
            logger.warning(f"[{self.username}] Sessão expirada. Fazendo login completo...")
        except Exception as e:
            logger.warning(f"[{self.username}] Erro ao restaurar sessão: {e}")

        return self._full_login()

    def _full_login(self) -> bool:
        try:
            self.cl.login(self.username, self.password)
            self._save_session()
            logger.info(f"[{self.username}] Login completo realizado.")
            return True
        except ChallengeRequired:
            logger.error(f"[{self.username}] Desafio de segurança exigido pelo Instagram.")
            return False
        except PleaseWaitFewMinutes:
            logger.error(f"[{self.username}] Instagram pedindo espera. Conta temporariamente bloqueada.")
            return False
        except Exception as e:
            logger.error(f"[{self.username}] Falha no login: {e}")
            return False

    def _save_session(self):
        self.cl.dump_settings(str(self.session_path))
        logger.debug(f"[{self.username}] Sessão salva em {self.session_path}")

    def save_session(self):
        """Exposto para chamada externa (ex: após cada ação bem-sucedida)."""
        self._save_session()

    def get_session_data(self) -> dict:
        """Retorna dados da sessão como dict (para backup no Supabase)."""
        if self.session_path.exists():
            with open(self.session_path, "r") as f:
                return json.load(f)
        return {}

    def load_session_from_data(self, data: dict):
        """Restaura sessão a partir de dict (vindo do Supabase)."""
        settings_path = str(self.session_path)
        with open(settings_path, "w") as f:
            json.dump(data, f)
        self.cl.load_settings(settings_path)

    # ─── Verificação de saúde ────────────────────────────────

    def is_logged_in(self) -> bool:
        try:
            self.cl.get_timeline_feed()
            return True
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        """Garante sessão ativa antes de qualquer ação."""
        if not self.is_logged_in():
            logger.warning(f"[{self.username}] Sessão inativa. Tentando reconectar...")
            return self.login()
        return True

    # ─── Propriedade do cliente bruto ────────────────────────

    @property
    def api(self) -> Client:
        """Acesso ao client instagrapi bruto para os outros módulos."""
        return self.cl
