import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.interval import IntervalTrigger

from config import (
    STORY_MONITOR_DELAY_MAX,
    STORY_MONITOR_DELAY_MIN,
    STORY_MONITOR_ENABLED,
    STORY_MONITOR_FALLBACK_BATCH,
    STORY_MONITOR_FOLLOWING_REFRESH_SECONDS,
    STORY_MONITOR_INTERVAL_SECONDS,
)
from database.accounts import AccountsDB
from database.operations import DB
from instagram.risk_detector import risk_detector
from instagram.stories import StoriesViewer
from scheduler.jobs import _authenticate_account, _backup_current_session

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

_STATE: dict[str, dict] = {}
_STATE_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


def _get_state(username: str) -> dict:
    key = username.casefold()
    with _STATE_GUARD:
        state = _STATE.get(key)
        if state is None:
            state = {
                "following_ids": [],
                "following_refreshed_at": 0.0,
                "cursor": 0,
                "tray_markers": {},
            }
            _STATE[key] = state
        return state


def _get_monitor_lock(username: str) -> threading.Lock:
    key = username.casefold()
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ACCOUNT_LOCKS[key] = lock
        return lock


@contextmanager
def _monitor_guard(username: str):
    """Impede dois ciclos de stories simultaneos para a mesma conta.

    Este lock e propositalmente separado do lock de follow/unfollow. Jobs de
    follow podem durar muitos minutos por causa dos delays; stories sao apenas
    leitura + seen e precisam continuar sendo monitorados nesse intervalo.
    """
    lock = _get_monitor_lock(username)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _next_round_robin(state: dict, batch_size: int) -> list[str]:
    ids = list(state.get("following_ids") or [])
    if not ids or batch_size <= 0:
        return []

    size = min(len(ids), int(batch_size))
    cursor = int(state.get("cursor", 0) or 0) % len(ids)
    batch = [ids[(cursor + offset) % len(ids)] for offset in range(size)]
    state["cursor"] = (cursor + size) % len(ids)
    return batch


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _session_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "login_required" in text or "loginrequired" in text


def _refresh_following_if_needed(viewer: StoriesViewer, state: dict) -> bool:
    now = time.monotonic()
    current = list(state.get("following_ids") or [])
    last_refresh = float(state.get("following_refreshed_at", 0.0) or 0.0)
    if current and now - last_refresh < STORY_MONITOR_FOLLOWING_REFRESH_SECONDS:
        return False

    following_ids = viewer.get_following_user_ids()
    state["following_ids"] = following_ids
    state["following_refreshed_at"] = now
    if following_ids:
        state["cursor"] = int(state.get("cursor", 0) or 0) % len(following_ids)
    else:
        state["cursor"] = 0
    logger.info(
        "[%s] Monitor de stories atualizou seguidos: %s perfil(is).",
        viewer.username,
        len(following_ids),
    )
    return True


def _run_story_monitor_job_sync() -> dict:
    """Executa um ciclo 24/7 para todas as contas ativas.

    Caminho rapido: o tray informa quais seguidos possuem story ativo e um
    marcador muda quando aparece story novo. Caminho de garantia: em todo ciclo
    uma parte da lista de seguidos e consultada diretamente em round-robin.
    """
    accounts_db = AccountsDB()
    db = DB()
    summary = {
        "accounts": 0,
        "viewed": 0,
        "errors": 0,
        "checked_users": 0,
    }

    for acc in accounts_db.list_active_accounts():
        username = acc["username"]
        if risk_detector.is_paused(username):
            continue

        with _monitor_guard(username) as acquired:
            if not acquired:
                logger.debug("[%s] Ciclo de stories anterior ainda ativo; pulando.", username)
                continue

            ig = _authenticate_account(acc, accounts_db)
            if not ig:
                continue
            viewer = StoriesViewer(ig, risk_detector)
            state = _get_state(username)

            try:
                refreshed = _refresh_following_if_needed(viewer, state)
            except Exception as exc:
                if _session_error(exc):
                    risk_detector.notify_session_expired(username)
                    continue
                logger.warning(
                    "[%s] Nao foi possivel atualizar lista de seguidos: %s",
                    username,
                    type(exc).__name__,
                )
                refreshed = False
                if not state.get("following_ids"):
                    continue

            following_ids = list(state.get("following_ids") or [])
            if not following_ids:
                logger.debug("[%s] Nenhum perfil seguido para monitorar.", username)
                continue
            allowed = set(following_ids)

            tray_markers: dict[str, str | None] = {}
            tray_candidates: list[str] = []
            try:
                tray_markers = viewer.get_tray_user_markers(allowed)
                previous_markers = state.setdefault("tray_markers", {})
                for uid, marker in tray_markers.items():
                    # Sem marcador confiavel, consultar sempre. Com marcador,
                    # consultar imediatamente quando ele mudar.
                    if marker is None or previous_markers.get(uid) != marker:
                        tray_candidates.append(uid)
            except Exception as exc:
                if _session_error(exc):
                    risk_detector.notify_session_expired(username)
                    continue
                logger.warning(
                    "[%s] Tray de stories indisponivel (%s); usando varredura.",
                    username,
                    type(exc).__name__,
                )

            fallback = _next_round_robin(state, STORY_MONITOR_FALLBACK_BATCH)
            targets = _dedupe(tray_candidates + fallback)
            if not targets:
                summary["accounts"] += 1
                continue

            result = viewer.view_stories_for_users(
                targets,
                max_per_run=len(targets),
                delay_min=STORY_MONITOR_DELAY_MIN,
                delay_max=STORY_MONITOR_DELAY_MAX,
            )

            failed = set(result.get("failed_user_ids") or [])
            story_users = set(result.get("story_user_ids") or []) - failed
            stored_markers = state.setdefault("tray_markers", {})
            for uid in story_users:
                if uid in tray_markers:
                    stored_markers[uid] = tray_markers[uid]

            # Quando a lista de seguidos e renovada, descarta marcadores de quem
            # deixou de ser seguido para o estado nao crescer indefinidamente.
            if refreshed:
                state["tray_markers"] = {
                    uid: marker
                    for uid, marker in stored_markers.items()
                    if uid in allowed
                }

            viewed = int(result.get("viewed", 0) or 0)
            errors = int(result.get("errors", 0) or 0)
            if viewed:
                db.log_action(
                    acc["id"],
                    "story_view",
                    detail=str(viewed),
                    success=True,
                )
                accounts_db.update_last_active(username)
                _backup_current_session(accounts_db, ig)
            if errors:
                db.log_action(
                    acc["id"],
                    "error",
                    detail=f"story_monitor_errors:{errors}",
                    success=False,
                )

            summary["accounts"] += 1
            summary["viewed"] += viewed
            summary["errors"] += errors
            summary["checked_users"] += len(targets)
            logger.info(
                "[%s] Monitor stories — seguidos=%s tray_novos=%s "
                "varredura=%s checados=%s vistos=%s erros=%s",
                username,
                len(following_ids),
                len(tray_candidates),
                len(fallback),
                len(targets),
                viewed,
                errors,
            )

    return summary


async def run_story_monitor_job() -> dict:
    return await asyncio.to_thread(_run_story_monitor_job_sync)


def attach_story_monitor(scheduler) -> bool:
    """Registra o monitor continuo no scheduler principal."""
    if not STORY_MONITOR_ENABLED:
        logger.info("Monitor continuo de stories desativado por configuracao.")
        return False

    if scheduler.get_job("story_monitor"):
        return True

    scheduler.add_job(
        run_story_monitor_job,
        IntervalTrigger(seconds=STORY_MONITOR_INTERVAL_SECONDS),
        id="story_monitor",
        next_run_time=datetime.now(LOCAL_TZ) + timedelta(seconds=5),
        coalesce=True,
        max_instances=1,
        misfire_grace_time=max(30, STORY_MONITOR_INTERVAL_SECONDS),
    )
    logger.info(
        "Monitor continuo de stories registrado: intervalo=%ss, "
        "refresh_seguidos=%ss, varredura=%s/ciclo.",
        STORY_MONITOR_INTERVAL_SECONDS,
        STORY_MONITOR_FOLLOWING_REFRESH_SECONDS,
        STORY_MONITOR_FALLBACK_BATCH,
    )
    return True
