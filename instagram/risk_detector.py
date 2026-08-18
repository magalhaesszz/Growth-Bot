import logging
from collections import deque
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from config import (
    RISK_ERROR_RATE_THRESHOLD,
    RISK_MIN_ACTIONS_TO_EVAL,
    ANOMALY_ZERO_ACTION_HOURS,
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


class RiskDetector:
    """
    Monitora sinais de risco por conta e decide pausas automáticas.
    Deve ser chamado antes e depois de cada ação do motor.
    """

    def __init__(self):
        self._states: dict[str, RiskState] = {}
        self._notify_fn = None  # callback async(mensagem) para alertar via Telegram

    def set_notify_fn(self, fn):
        """Define a funcao de notificacao (chamada quando uma conta e pausada)."""
        self._notify_fn = fn

    def _state(self, username: str) -> RiskState:
        if username not in self._states:
            self._states[username] = RiskState(username=username)
        return self._states[username]

    # ─── Registro de resultado de ação ───────────────────────

    def record_success(self, username: str):
        state = self._state(username)
        state.action_window.append(True)
        state.last_action_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        state.consecutive_errors = 0

    def record_error(self, username: str, error: Exception):
        state = self._state(username)
        state.action_window.append(False)
        state.last_action_at = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        state.consecutive_errors += 1

        error_str = str(error).lower()

        # Sinais críticos — pausa imediata
        if any(kw in error_str for kw in [
            "challenge", "checkpoint", "feedback_required",
            "please wait", "action_blocked", "spam"
        ]):
            self._pause(username, f"Sinal crítico detectado: {type(error).__name__}")
            state.challenge_detected = "challenge" in error_str or "checkpoint" in error_str
            return

        # 3 erros seguidos → pausa
        if state.consecutive_errors >= 3:
            self._pause(username, f"{state.consecutive_errors} erros consecutivos")
            return

        # Taxa de erro alta na janela
        self._eval_error_rate(username)

    # ─── Avaliação de taxa de erro ───────────────────────────

    def _eval_error_rate(self, username: str):
        state = self._state(username)
        window = list(state.action_window)

        if len(window) < RISK_MIN_ACTIONS_TO_EVAL:
            return

        error_rate = window.count(False) / len(window)
        if error_rate >= RISK_ERROR_RATE_THRESHOLD:
            self._pause(
                username,
                f"Taxa de erro alta: {error_rate:.0%} nas últimas {len(window)} ações"
            )

    # ─── Detecção de anomalia (0 ações na janela) ────────────

    def check_anomaly(self, username: str, hour_start: int, hour_end: int) -> bool:
        """
        Retorna True se o bot deveria ter agido mas não agiu.
        Chame periodicamente (ex: a cada 30 min pelo scheduler).
        """
        state = self._state(username)
        now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        hour_now = now.hour

        if not (hour_start <= hour_now < hour_end):
            return False  # fora da janela de operação, ok

        if state.last_action_at is None:
            return False  # ainda não começou nada hoje

        idle_hours = (now - state.last_action_at).total_seconds() / 3600
        if idle_hours >= ANOMALY_ZERO_ACTION_HOURS:
            logger.warning(
                f"[{username}] Anomalia: {idle_hours:.1f}h sem ação dentro da janela operacional."
            )
            return True

        return False

    # ─── Pausa / retomada ────────────────────────────────────

    def _pause(self, username: str, reason: str):
        state = self._state(username)
        if not state.is_paused:
            state.is_paused = True
            state.pause_reason = reason
            logger.error(f"[{username}] CONTA PAUSADA — {reason}")
            if self._notify_fn:
                try:
                    import asyncio
                    msg = f"🚨 *Conta pausada automaticamente*\n\n@{username}\nMotivo: {reason}"
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(self._notify_fn(msg), loop)
                    else:
                        asyncio.run(self._notify_fn(msg))
                except Exception as e:
                    logger.warning(f"Falha ao notificar pausa de risco: {e}")

    def notify_session_expired(self, username: str):
        """Notifica que a sessao expirou e pausa a conta ate reconectar."""
        state = self._state(username)
        if not state.is_paused:
            state.is_paused = True
            state.pause_reason = "Sessão expirada"
            logger.error(f"[{username}] SESSAO EXPIRADA — conta pausada.")
            if self._notify_fn:
                try:
                    import asyncio
                    msg = (
                        f"🔒 *Sessão expirada — @{username}*\n\n"
                        f"O Instagram desconectou a conta.\n"
                        f"Use `/conta_sessao @{username} SESSIONID` para reconectar."
                    )
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(self._notify_fn(msg), loop)
                    else:
                        asyncio.run(self._notify_fn(msg))
                except Exception as e:
                    logger.warning(f"Falha ao notificar sessao expirada: {e}")

    def resume(self, username: str):
        state = self._state(username)
        state.is_paused = False
        state.pause_reason = ""
        state.consecutive_errors = 0
        state.challenge_detected = False
        logger.info(f"[{username}] Conta retomada manualmente.")

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
            "last_action_at": state.last_action_at.isoformat() if state.last_action_at else None,
        }

    def get_all_statuses(self) -> list[dict]:
        return [self.get_status(u) for u in self._states]


# Uma única instância deve ser usada pelo scheduler e pelo painel.
risk_detector = RiskDetector()
