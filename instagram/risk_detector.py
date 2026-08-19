import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    ANOMALY_ZERO_ACTION_HOURS,
    RISK_ERROR_RATE_THRESHOLD,
    RISK_MIN_ACTIONS_TO_EVAL,
)

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


@dataclass
class RiskState:
    username: str
    action_window: deque = field(default_factory=lambda: deque(maxlen=50))
    last_action_at: datetime | None = None
    is_paused: bool = False
    pause_reason: str = ""
    challenge_detected: bool = False
    consecutive_errors: int = 0
    anomaly_alerted: bool = False


class RiskDetector:
    """Monitora risco por conta e pausa antes de insistir em erros.

    O estado de pausa pode ser persistido por callbacks configurados no startup.
    Assim uma reinicializacao nao libera silenciosamente uma conta que havia sido
    pausada. A funcao de notificacao tambem recebe o event loop principal para
    continuar funcionando quando o detector e chamado dentro de ``to_thread``.
    """

    def __init__(self):
        self._states: dict[str, RiskState] = {}
        self._loaded: set[str] = set()
        self._notify_fn = None
        self._notify_loop: asyncio.AbstractEventLoop | None = None
        self._load_state_fn = None
        self._save_state_fn = None

    def set_notify_fn(self, fn, loop: asyncio.AbstractEventLoop | None = None):
        self._notify_fn = fn
        self._notify_loop = loop

    def set_persistence(self, load_fn=None, save_fn=None):
        """Configura callbacks sincronicos ``load(username)`` e ``save(username, data)``."""
        self._load_state_fn = load_fn
        self._save_state_fn = save_fn
        self._loaded.clear()

    def _snapshot(self, state: RiskState) -> dict:
        return {
            "is_paused": state.is_paused,
            "pause_reason": state.pause_reason,
            "challenge_detected": state.challenge_detected,
            "consecutive_errors": state.consecutive_errors,
            "anomaly_alerted": state.anomaly_alerted,
        }

    def _persist(self, state: RiskState) -> None:
        if not self._save_state_fn:
            return
        try:
            self._save_state_fn(state.username, self._snapshot(state))
        except Exception as exc:
            logger.warning(
                "[%s] Nao foi possivel persistir estado de risco: %s",
                state.username,
                type(exc).__name__,
            )

    def _load_persisted(self, state: RiskState) -> None:
        username = state.username
        if username in self._loaded:
            return
        self._loaded.add(username)
        if not self._load_state_fn:
            return
        try:
            saved = self._load_state_fn(username) or {}
        except Exception as exc:
            logger.warning(
                "[%s] Nao foi possivel restaurar estado de risco: %s",
                username,
                type(exc).__name__,
            )
            return
        if not isinstance(saved, dict):
            return
        state.is_paused = bool(saved.get("is_paused", state.is_paused))
        state.pause_reason = str(saved.get("pause_reason", state.pause_reason) or "")
        state.challenge_detected = bool(
            saved.get("challenge_detected", state.challenge_detected)
        )
        try:
            state.consecutive_errors = max(
                0, int(saved.get("consecutive_errors", state.consecutive_errors))
            )
        except (TypeError, ValueError):
            pass
        state.anomaly_alerted = bool(saved.get("anomaly_alerted", False))

    def _state(self, username: str) -> RiskState:
        key = username.strip().lstrip("@")
        if key not in self._states:
            self._states[key] = RiskState(username=key)
        state = self._states[key]
        self._load_persisted(state)
        return state

    def _dispatch_notification(self, message: str) -> None:
        if not self._notify_fn:
            return
        try:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            target_loop = self._notify_loop
            if current_loop is not None and (
                target_loop is None or current_loop is target_loop
            ):
                current_loop.create_task(self._notify_fn(message))
                return

            if target_loop is not None and target_loop.is_running():
                asyncio.run_coroutine_threadsafe(self._notify_fn(message), target_loop)
                return

            # Fallback para usos fora do bot principal, como scripts locais.
            asyncio.run(self._notify_fn(message))
        except Exception as exc:
            logger.warning("Falha ao notificar pausa de risco: %s", type(exc).__name__)

    # ─── Registro de resultado de acao ───────────────────────

    def record_success(self, username: str):
        state = self._state(username)
        state.action_window.append(True)
        state.last_action_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        state.consecutive_errors = 0
        if state.anomaly_alerted:
            state.anomaly_alerted = False
            self._persist(state)

    def record_error(self, username: str, error: Exception):
        state = self._state(username)
        state.action_window.append(False)
        state.last_action_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        state.consecutive_errors += 1
        if state.anomaly_alerted:
            state.anomaly_alerted = False

        error_str = str(error).lower()
        critical = any(
            kw in error_str
            for kw in (
                "challenge",
                "checkpoint",
                "feedback_required",
                "please wait",
                "action_blocked",
                "spam",
            )
        )
        if critical:
            state.challenge_detected = (
                "challenge" in error_str or "checkpoint" in error_str
            )
            self._pause(
                username, f"Sinal critico detectado: {type(error).__name__}"
            )
            return

        if state.consecutive_errors >= 3:
            self._pause(username, f"{state.consecutive_errors} erros consecutivos")
            return

        self._eval_error_rate(username)

    def _eval_error_rate(self, username: str):
        state = self._state(username)
        window = list(state.action_window)
        if len(window) < RISK_MIN_ACTIONS_TO_EVAL:
            return
        error_rate = window.count(False) / len(window)
        if error_rate >= RISK_ERROR_RATE_THRESHOLD:
            self._pause(
                username,
                f"Taxa de erro alta: {error_rate:.0%} nas ultimas {len(window)} acoes",
            )

    # ─── Deteccao de anomalia ────────────────────────────────

    def check_anomaly(self, username: str, hour_start: int, hour_end: int) -> bool:
        """Retorna ``True`` apenas na primeira deteccao do mesmo periodo ocioso."""
        state = self._state(username)
        if state.is_paused:
            return False

        now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        if not (hour_start <= now.hour < hour_end):
            return False
        if state.last_action_at is None:
            return False
        # Nao transforme a ultima acao de ontem em alerta imediato hoje.
        if state.last_action_at.date() != now.date():
            return False

        idle_hours = (now - state.last_action_at).total_seconds() / 3600
        if idle_hours < ANOMALY_ZERO_ACTION_HOURS:
            return False
        if state.anomaly_alerted:
            return False

        state.anomaly_alerted = True
        self._persist(state)
        logger.warning(
            "[%s] Anomalia: %.1fh sem acao dentro da janela operacional.",
            username,
            idle_hours,
        )
        return True

    # ─── Pausa / retomada ────────────────────────────────────

    def _pause(self, username: str, reason: str) -> bool:
        state = self._state(username)
        if state.is_paused:
            return False
        state.is_paused = True
        state.pause_reason = reason
        self._persist(state)
        logger.error("[%s] CONTA PAUSADA — %s", username, reason)
        self._dispatch_notification(
            f"🚨 *Conta pausada automaticamente*\n\n@{username}\nMotivo: {reason}"
        )
        return True

    def pause(self, username: str, reason: str) -> bool:
        return self._pause(username, reason)

    def notify_session_expired(self, username: str) -> bool:
        """Pausa e avisa uma unica vez ate a sessao ser renovada/retomada."""
        state = self._state(username)
        if state.is_paused:
            return False
        state.is_paused = True
        state.pause_reason = "Sessão expirada"
        state.challenge_detected = False
        self._persist(state)
        logger.error("[%s] SESSAO EXPIRADA — conta pausada.", username)
        self._dispatch_notification(
            f"🔒 *Sessão expirada — @{username}*\n\n"
            "O Instagram desconectou a conta.\n"
            f"Use `/conta_sessao @{username} SESSIONID` para reconectar."
        )
        return True

    def resume(self, username: str):
        state = self._state(username)
        state.is_paused = False
        state.pause_reason = ""
        state.consecutive_errors = 0
        state.challenge_detected = False
        state.anomaly_alerted = False
        self._persist(state)
        logger.info("[%s] Conta retomada.", username)

    # ─── Consulta de estado ──────────────────────────────────

    def is_paused(self, username: str) -> bool:
        return self._state(username).is_paused

    def get_status(self, username: str) -> dict:
        state = self._state(username)
        window = list(state.action_window)
        total = len(window)
        errors = window.count(False)
        return {
            "username": username,
            "is_paused": state.is_paused,
            "pause_reason": state.pause_reason,
            "challenge_detected": state.challenge_detected,
            "consecutive_errors": state.consecutive_errors,
            "error_rate": f"{errors / total:.0%}" if total else "N/A",
            "actions_in_window": total,
            "last_action_at": (
                state.last_action_at.isoformat() if state.last_action_at else None
            ),
            "anomaly_alerted": state.anomaly_alerted,
        }

    def get_all_statuses(self) -> list[dict]:
        return [self.get_status(username) for username in list(self._states)]

    def reset(self, username: str | None = None) -> None:
        """Limpa somente o cache em memoria; util para testes/reload controlado."""
        if username is None:
            self._states.clear()
            self._loaded.clear()
            return
        key = username.strip().lstrip("@")
        self._states.pop(key, None)
        self._loaded.discard(key)


risk_detector = RiskDetector()
