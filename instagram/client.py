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
from instagrapi.mixins.challenge import ChallengeChoice

from config import SESSIONS_DIR

logger = logging.getLogger(__name__)

# Estado de challenges pendentes
# {username: {"client": InstagramClient, "code": None|str, "type": str, "choice": None|str}}
PENDING_CHALLENGES: dict[str, dict] = {}


class InstagramClient:
    def __init__(self, username: str, password: str, device_fingerprint: dict = None):
        self.username = username
        self.password = password
        self.session_path = Path(SESSIONS_DIR) / f"{username}.json"
        self._fingerprint = {"device": "Pixel 8 Pro (padrão instagrapi)"}
        self.cl = self._build_client()

    def _build_client(self) -> Client:
        cl = Client()
        # Usa dispositivo padrão do instagrapi (versão sempre atualizada)
        # Apenas randomiza UUIDs para anti-ban
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
        """
        Chamado pelo instagrapi quando precisa de um código.
        choice pode ser: ChallengeChoice.EMAIL, ChallengeChoice.SMS, ou None
        Aguarda até 5 minutos pelo código digitado no Telegram.
        """
        choice_label = {
            ChallengeChoice.EMAIL: "email",
            ChallengeChoice.SMS:   "sms",
        }.get(choice, "email_ou_sms")

        logger.info(f"[{username}] Aguardando código via Telegram (método: {choice_label})...")

        entry = PENDING_CHALLENGES.get(username, {})
        entry.update({"client": self, "code": None, "choice": choice_label})
        PENDING_CHALLENGES[username] = entry

        timeout = 300
        elapsed = 0
        while elapsed < timeout:
            e = PENDING_CHALLENGES.get(username)
            if e and e.get("code"):
                code = e["code"]
                PENDING_CHALLENGES.pop(username, None)
                logger.info(f"[{username}] Código recebido: {code}")
                return str(code)
            time.sleep(2)
            elapsed += 2

        PENDING_CHALLENGES.pop(username, None)
        logger.error(f"[{username}] Timeout aguardando código.")
        return ""

    # ─── Seleção de método pelo usuário ──────────────────────

    def request_method_selection(self) -> dict:
        """
        Retorna os métodos disponíveis para verificação.
        Deve ser chamado antes de iniciar o challenge_flow
        para permitir que o usuário escolha email ou SMS.
        """
        try:
            last = self.cl.last_json or {}
            methods = []

            # Verificar métodos disponíveis no last_json
            contact_point = last.get("step_data", {})
            if contact_point.get("email"):
                methods.append({
                    "type": "email",
                    "label": f"Email ({contact_point['email']})",
                    "choice": ChallengeChoice.EMAIL,
                })
            if contact_point.get("phone_number"):
                methods.append({
                    "type": "sms",
                    "label": f"SMS ({contact_point['phone_number']})",
                    "choice": ChallengeChoice.SMS,
                })

            # Fallback: oferecer ambos se não tiver informação específica
            if not methods:
                methods = [
                    {"type": "email", "label": "Email", "choice": ChallengeChoice.EMAIL},
                    {"type": "sms",   "label": "SMS",   "choice": ChallengeChoice.SMS},
                    {"type": "backup", "label": "Código de backup (8 dígitos)", "choice": None},
                ]

            return {"ok": True, "methods": methods}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start_challenge_with_method(self, method_type: str) -> str:
        """
        Inicia o challenge com o método escolhido pelo usuário.
        method_type: 'email', 'sms', 'backup'
        """
        try:
            last = self.cl.last_json or {}
            logger.info(f"[{self.username}] Iniciando challenge com método: {method_type}")

            if method_type == "backup":
                # Código de backup — não precisa solicitar envio, só aguardar o código
                PENDING_CHALLENGES[self.username] = {
                    "client": self, "code": None,
                    "choice": "backup", "type": "backup"
                }
                return "challenge"

            choice = ChallengeChoice.EMAIL if method_type == "email" else ChallengeChoice.SMS

            # Tentar usar challenge_resolve para selecionar o método
            try:
                challenge_url = last.get("challenge", {}).get("api_path", "")
                if challenge_url:
                    self.cl.private.post(
                        challenge_url.replace("/api/v1", ""),
                        data={"choice": str(choice.value if hasattr(choice, 'value') else choice)},
                    )
            except Exception as e:
                logger.warning(f"[{self.username}] Não conseguiu selecionar método via POST: {e}")

            # Iniciar o challenge_flow — ele vai chamar _telegram_code_handler
            PENDING_CHALLENGES[self.username] = {
                "client": self, "code": None,
                "choice": method_type, "type": "challenge"
            }
            result = self.cl.challenge_flow(last)
            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return "ok"
            return "challenge"

        except Exception as e:
            logger.error(f"[{self.username}] Erro ao iniciar challenge com método {method_type}: {e}")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None, "choice": method_type}
            return "challenge"

    # ─── Login ───────────────────────────────────────────────

    def login(self) -> str:
        """Retorna: 'ok' | 'challenge' | 'select_method' | 'two_factor' | 'error:motivo'"""

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

            if any(x in err_str for x in ["email", "send you", "get back", "upgrade", "verify"]):
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
        except Exception as e:
            err_str = str(e).lower()
            # 429 Too Many Requests — rate limit por IP
            if "429" in err_str or "too many" in err_str or "retry" in err_str.lower():
                logger.error(f"[{self.username}] Rate limit 429 — aguarde 30-60 minutos antes de tentar novamente.")
                return "error:rate_limit_429"
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
            return "challenge"
        except Exception as e:
            logger.error(f"[{self.username}] Erro no challenge_flow: {e}")
            PENDING_CHALLENGES[self.username] = {"client": self, "code": None}
            return "challenge"

    # ─── Submissão de códigos ────────────────────────────────

    def submit_code(self, code: str) -> bool:
        """Injeta o código no handler que está aguardando."""
        code = str(code).strip()
        entry = PENDING_CHALLENGES.get(self.username)
        if entry is not None:
            entry["code"] = code
            return True
        return False

    def submit_backup_code(self, code: str) -> bool:
        """Submete código de backup de 8 dígitos diretamente."""
        try:
            result = self.cl.login(self.username, self.password,
                                   verification_code=code.strip())
            if result:
                self._save_session()
                PENDING_CHALLENGES.pop(self.username, None)
                return True
        except Exception as e:
            logger.error(f"[{self.username}] Erro no backup code: {e}")
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
