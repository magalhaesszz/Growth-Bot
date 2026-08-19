import asyncio
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import TELEGRAM_OWNER_ID
from database.accounts import AccountsDB
from database.operations import DB
from database.state import BotStateDB
from instagram.client import InstagramClient
from instagram.follower import Follower
from instagram.risk_detector import risk_detector
from instagram.score import BlacklistFilter, ProfileScorer, WhitelistFilter
from instagram.scraper import Scraper
from instagram.stories import StoriesViewer
from instagram.unfollower import Unfollower
from scheduler.anomaly import check_all_anomalies
from scheduler.warmup import advance_all_warmups, get_warmup_limit

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

_telegram_app = None

_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()

_MANUAL_MODE = False
_MANUAL_TASK: asyncio.Task | None = None
_MANUAL_STOP_EVENT = threading.Event()
_MANUAL_WAKE_EVENT: asyncio.Event | None = None


def _get_account_lock(username: str) -> threading.Lock:
    key = username.casefold()
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ACCOUNT_LOCKS[key] = lock
        return lock


@contextmanager
def _account_guard(username: str):
    """Serializa todas as acoes Instagram de uma mesma conta no processo."""
    lock = _get_account_lock(username)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


async def _notify(message: str):
    if not _telegram_app:
        return
    try:
        await _telegram_app.bot.send_message(
            chat_id=TELEGRAM_OWNER_ID,
            text=message,
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("Erro ao enviar notificacao Telegram: %s", exc)


def _get_daily_limit(account: dict) -> int:
    warmup_day = int(account.get("warmup_day", 0) or 0)
    if warmup_day > 0:
        return get_warmup_limit(warmup_day)
    return int(account.get("daily_follows", 40) or 0)


def _backup_current_session(accounts_db: AccountsDB, ig: InstagramClient) -> None:
    try:
        data = ig.get_session_data()
        if data:
            accounts_db.save_session_backup(ig.username, data)
    except Exception as exc:
        logger.warning(
            "[%s] Nao foi possivel atualizar backup de sessao: %s",
            ig.username,
            type(exc).__name__,
        )


def _authenticate_account(acc: dict, accounts_db: AccountsDB) -> InstagramClient | None:
    """Restaura a sessao, valida ao vivo e so entao tenta senha como fallback."""
    username = acc["username"]
    ig = InstagramClient(
        username,
        acc.get("password", ""),
        acc.get("fingerprint"),
    )

    session = accounts_db.load_session_backup(username)
    if session:
        try:
            ig.load_session_from_data(session)
            if ig.is_logged_in():
                status = risk_detector.get_status(username)
                if status["is_paused"] and status["pause_reason"] == "Sessão expirada":
                    risk_detector.resume(username)
                return ig
        except Exception as exc:
            logger.warning(
                "[%s] Backup de sessao nao validou: %s",
                username,
                type(exc).__name__,
            )

    password = acc.get("password", "") or ""
    if password:
        result = ig.login()
        if result == "ok":
            _backup_current_session(accounts_db, ig)
            return ig
        logger.warning("[%s] Fallback de login retornou %s", username, result)

    risk_detector.notify_session_expired(username)
    return None


def _run_follow_job_sync(ignore_schedule: bool = False, stop_event=None) -> dict:
    accounts_db = AccountsDB()
    db = DB()
    summary = {"followed": 0, "stories": 0, "accounts": 0}

    for acc in accounts_db.list_active_accounts():
        if stop_event is not None and stop_event.is_set():
            break
        username = acc["username"]

        with _account_guard(username) as acquired:
            if not acquired:
                logger.info("[%s] Outro job ja esta usando a conta; pulando.", username)
                continue
            if risk_detector.is_paused(username):
                logger.warning("[%s] Pausada por risco; follow bloqueado.", username)
                continue

            if not ignore_schedule:
                now_hour = datetime.now(LOCAL_TZ).hour
                if not (int(acc["hour_start"]) <= now_hour < int(acc["hour_end"])):
                    continue

            configured_limit = _get_daily_limit(acc)
            already_today = db.count_today_follows(acc["id"])
            run_limit = max(0, configured_limit - already_today)
            if run_limit <= 0:
                logger.info(
                    "[%s] Limite diario ja atingido (%s/%s).",
                    username,
                    already_today,
                    configured_limit,
                )
                continue

            ig = _authenticate_account(acc, accounts_db)
            if not ig:
                continue

            targets = db.list_targets(acc["id"])
            if not targets:
                logger.info("[%s] Sem alvos cadastrados.", username)
                continue

            already_following = db.get_already_following_ids(acc["id"])
            follower = Follower(
                ig,
                risk_detector,
                ProfileScorer(),
                BlacklistFilter(db.get_blacklist(acc["id"])),
            )
            campaign = db.get_active_campaign(acc["id"])
            campaign_id = campaign["id"] if campaign else None
            total_followed = 0

            for target in targets:
                if stop_event is not None and stop_event.is_set():
                    break
                if total_followed >= run_limit or risk_detector.is_paused(username):
                    break

                scraper = Scraper(ig)
                if not target.get("page_user_id"):
                    page = scraper.resolve_page(target["page_url"])
                    if not page:
                        continue
                    db.sb.table("ig_targets").update(
                        {
                            "page_username": page["username"],
                            "page_user_id": page["user_id"],
                        }
                    ).eq("id", target["id"]).execute()
                    target.update(page)

                profiles = scraper.get_followers(
                    target["page_user_id"],
                    target["page_username"],
                    limit=150,
                    already_following=already_following,
                    stop_event=stop_event,
                )
                db.update_target_scraped(target["id"], len(profiles))

                def on_follow(uname, uid, _acc=acc, _campaign_id=campaign_id):
                    db.add_followed(_acc["id"], uid, uname, _campaign_id)
                    already_following.add(str(uid))
                    db.log_action(_acc["id"], "follow", uname, success=True)
                    if _campaign_id:
                        db.update_campaign_stats(_campaign_id, follows=1)
                    accounts_db.update_last_active(_acc["username"])

                result = follower.follow_batch(
                    profiles,
                    daily_limit=run_limit,
                    min_score=int(acc.get("score_min", 50)),
                    delay_min=int(acc.get("delay_min", 30)),
                    delay_max=int(acc.get("delay_max", 90)),
                    on_success=on_follow,
                    stop_event=stop_event,
                )
                total_followed += result["followed"]
                if result.get("stopped"):
                    break

            if (
                not risk_detector.is_paused(username)
                and not (stop_event is not None and stop_event.is_set())
            ):
                recent_ids = list(db.get_already_following_ids(acc["id"]))[:30]
                if recent_ids:
                    viewer = StoriesViewer(ig, risk_detector)
                    story_result = viewer.view_stories_for_users(
                        recent_ids,
                        max_per_run=20,
                        delay_min=5,
                        delay_max=15,
                        stop_event=stop_event,
                    )
                    if story_result["viewed"]:
                        db.log_action(
                            acc["id"],
                            "story_view",
                            detail=str(story_result["viewed"]),
                            success=True,
                        )
                    if story_result["errors"]:
                        db.log_action(
                            acc["id"],
                            "error",
                            detail=f"story_errors:{story_result['errors']}",
                            success=False,
                        )
                    summary["stories"] += story_result["viewed"]

            _backup_current_session(accounts_db, ig)
            summary["followed"] += total_followed
            summary["accounts"] += 1
            logger.info(
                "[%s] Follow job finalizado — %s follows.", username, total_followed
            )

    return summary


async def run_follow_job(ignore_schedule: bool = False, stop_event=None) -> dict:
    return await asyncio.to_thread(_run_follow_job_sync, ignore_schedule, stop_event)


def _run_unfollow_job_sync() -> dict:
    accounts_db = AccountsDB()
    db = DB()
    summary = {"unfollowed": 0}

    for acc in accounts_db.list_active_accounts():
        username = acc["username"]
        with _account_guard(username) as acquired:
            if not acquired or risk_detector.is_paused(username):
                continue

            remaining = max(
                0,
                int(acc.get("daily_unfollows", 40))
                - db.count_today_unfollows(acc["id"]),
            )
            if remaining <= 0:
                continue

            candidates = db.get_unfollow_candidates(
                acc["id"], int(acc.get("unfollow_after_days", 5))
            )
            if not candidates:
                continue

            ig = _authenticate_account(acc, accounts_db)
            if not ig:
                continue
            unfollower = Unfollower(
                ig,
                risk_detector,
                WhitelistFilter(db.get_whitelist(acc["id"])),
            )
            campaign = db.get_active_campaign(acc["id"])
            campaign_id = campaign["id"] if campaign else None
            known_follow_back = {
                str(item.get("target_username", "")): bool(item.get("follows_back"))
                for item in candidates
            }
            policy = acc.get("unfollow_policy", "keep_follow_backs")

            def on_unfollow(uname, uid, kept: bool, _acc=acc):
                if kept:
                    # Registra a conversao apenas na primeira confirmacao.
                    if policy == "keep_follow_backs" and not known_follow_back.get(uname):
                        db.mark_follows_back(_acc["id"], uname)
                        db.log_action(
                            _acc["id"], "follow_back_detected", uname, success=True
                        )
                        known_follow_back[uname] = True
                        if campaign_id:
                            db.update_campaign_stats(campaign_id, follow_backs=1)
                    return
                db.mark_unfollowed(_acc["id"], uname)
                db.log_action(_acc["id"], "unfollow", uname, success=True)
                if campaign_id:
                    db.update_campaign_stats(campaign_id, unfollows=1)

            result = unfollower.unfollow_batch(
                candidates,
                daily_limit=remaining,
                delay_min=int(acc.get("delay_min", 30)),
                delay_max=int(acc.get("delay_max", 90)),
                on_success=on_unfollow,
                policy=policy,
            )
            _backup_current_session(accounts_db, ig)
            summary["unfollowed"] += result["unfollowed"]

    return summary


async def run_unfollow_job() -> dict:
    return await asyncio.to_thread(_run_unfollow_job_sync)


def _auto_unfollow_follow_backs_sync() -> list[str]:
    accounts_db = AccountsDB()
    db = DB()
    messages: list[str] = []

    for acc in accounts_db.list_active_accounts():
        username = acc["username"]
        with _account_guard(username) as acquired:
            if not acquired or risk_detector.is_paused(username):
                continue
            remaining = max(
                0,
                int(acc.get("daily_unfollows", 40))
                - db.count_today_unfollows(acc["id"]),
            )
            if remaining <= 0:
                continue
            ig = _authenticate_account(acc, accounts_db)
            if not ig:
                continue
            unfollower = Unfollower(
                ig,
                risk_detector,
                WhitelistFilter(db.get_whitelist(acc["id"])),
            )
            count = unfollower.auto_unfollow_follow_backs(
                acc["id"],
                db,
                daily_limit=remaining,
                delay_min=int(acc.get("delay_min", 30)),
                delay_max=int(acc.get("delay_max", 90)),
                max_checks=50,
            )
            _backup_current_session(accounts_db, ig)
            if count:
                messages.append(
                    f"🔄 @{username}: {count} auto-unfollow(s) de follow-backs confirmados."
                )
    return messages


async def run_auto_unfollow_follow_backs():
    for message in await asyncio.to_thread(_auto_unfollow_follow_backs_sync):
        await _notify(message)


def _run_session_backup_job_sync():
    accounts_db = AccountsDB()
    for acc in accounts_db.list_active_accounts():
        ig = InstagramClient(
            acc["username"], acc.get("password", ""), acc.get("fingerprint")
        )
        _backup_current_session(accounts_db, ig)
    logger.info("Backup de sessoes concluido.")


async def run_session_backup_job():
    await asyncio.to_thread(_run_session_backup_job_sync)


async def run_daily_report_job():
    accounts_db = AccountsDB()
    db = DB()
    for acc in await asyncio.to_thread(accounts_db.list_active_accounts):
        if not acc.get("daily_report_enabled", True):
            continue
        stats = await asyncio.to_thread(db.get_stats_today, acc["id"])
        await _notify(
            f"📊 *Relatório diário — @{acc['username']}*\n\n"
            f"Follows: *{stats.get('follow', 0)}*\n"
            f"Unfollows: *{stats.get('unfollow', 0)}*\n"
            f"Stories: *{stats.get('story_view', 0)}*\n"
            f"Erros: *{stats.get('error', 0)}*"
        )


async def run_warmup_job():
    await advance_all_warmups(notify_fn=_notify)


async def run_anomaly_check():
    await check_all_anomalies(risk_detector, notify_fn=_notify)


async def run_weekly_report():
    try:
        from reports.daily import ReportGenerator

        accounts_db = AccountsDB()
        reporter = ReportGenerator()
        for acc in accounts_db.list_active_accounts():
            text = reporter.generate_text(acc["id"], acc["username"])
            chart = reporter.generate_chart(acc["id"], acc["username"])
            if _telegram_app:
                await _telegram_app.bot.send_photo(
                    chat_id=TELEGRAM_OWNER_ID,
                    photo=chart,
                    caption=text,
                    parse_mode="Markdown",
                )
    except Exception as exc:
        logger.error("Erro no relatorio semanal: %s", exc)


# ─── Modo manual ─────────────────────────────────────────────


def _persist_manual_state(active: bool):
    BotStateDB().set("manual_mode", "true" if active else "false")


def _read_manual_state() -> bool:
    return BotStateDB().get("manual_mode", "false") == "true"


async def run_manual_mode():
    global _MANUAL_MODE
    logger.info("Modo manual iniciado.")
    try:
        while _MANUAL_MODE and not _MANUAL_STOP_EVENT.is_set():
            summary = await run_follow_job(
                ignore_schedule=True, stop_event=_MANUAL_STOP_EVENT
            )
            if not _MANUAL_MODE or _MANUAL_STOP_EVENT.is_set():
                break
            delay = 10 if summary.get("followed", 0) else 60
            if _MANUAL_WAKE_EVENT is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(_MANUAL_WAKE_EVENT.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            _MANUAL_WAKE_EVENT.clear()
    finally:
        logger.info("Modo manual encerrado.")


async def start_manual_mode(telegram_app=None) -> bool:
    global _MANUAL_MODE, _MANUAL_TASK, _telegram_app, _MANUAL_WAKE_EVENT
    if _MANUAL_MODE:
        return False
    if telegram_app:
        _telegram_app = telegram_app
    _MANUAL_MODE = True
    _MANUAL_STOP_EVENT.clear()
    _MANUAL_WAKE_EVENT = asyncio.Event()
    _persist_manual_state(True)
    _MANUAL_TASK = asyncio.create_task(run_manual_mode())
    return True


async def stop_manual_mode() -> bool:
    global _MANUAL_MODE, _MANUAL_TASK
    if not _MANUAL_MODE:
        return False
    _MANUAL_MODE = False
    _MANUAL_STOP_EVENT.set()
    _persist_manual_state(False)
    if _MANUAL_WAKE_EVENT is not None:
        _MANUAL_WAKE_EVENT.set()

    task = _MANUAL_TASK
    if task and not task.done():
        try:
            # Nao cancela a coroutine que aguarda to_thread: o Event interrompe
            # delays e impede a proxima acao assim que a chamada atual terminar.
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(
                "Modo manual recebeu stop; uma chamada de rede ainda esta encerrando."
            )
    _MANUAL_TASK = None
    return True


async def resume_manual_mode_if_needed(telegram_app=None):
    if _read_manual_state():
        logger.info("Modo manual estava ativo antes do restart; retomando.")
        await start_manual_mode(telegram_app)
        await _notify(
            "🔄 *Modo manual retomado automaticamente*\n\n"
            "O bot reiniciou e retomou o estado salvo."
        )


def is_manual_mode() -> bool:
    return _MANUAL_MODE


# ─── Unfollow externo seguro ─────────────────────────────────


def _following_candidates(ig: InstagramClient, max_scan: int) -> list[dict]:
    try:
        raw = ig.api.user_following(ig.api.user_id, amount=max_scan)
    except TypeError:
        raw = ig.api.user_following(ig.api.user_id)
    items = []
    for uid, user in list(raw.items())[:max_scan]:
        items.append(
            {
                "target_user_id": str(uid),
                "target_username": str(getattr(user, "username", uid)),
            }
        )
    return items


def _count_external_nonfollowers_sync(username: str, max_scan: int = 200) -> dict:
    accounts_db = AccountsDB()
    db = DB()
    acc = accounts_db.get_account(username)
    if not acc:
        return {"ok": False, "error": "Conta nao encontrada"}

    with _account_guard(username) as acquired:
        if not acquired:
            return {"ok": False, "error": "Conta ocupada por outro job"}
        if risk_detector.is_paused(username):
            return {"ok": False, "error": "Conta pausada por risco"}
        ig = _authenticate_account(acc, accounts_db)
        if not ig:
            return {"ok": False, "error": "Sessao invalida"}
        checker = Unfollower(
            ig,
            risk_detector,
            WhitelistFilter(db.get_whitelist(acc["id"])),
        )
        candidates = _following_candidates(ig, max_scan)
        nonfollowers = followbacks = unknown = protected = 0
        for item in candidates:
            if checker.whitelist.is_protected(item["target_username"]):
                protected += 1
                continue
            relation = checker._follows_back(item["target_user_id"])
            if relation is True:
                followbacks += 1
            elif relation is False:
                nonfollowers += 1
            else:
                unknown += 1
        return {
            "ok": True,
            "checked": len(candidates),
            "nonfollowers": nonfollowers,
            "followbacks": followbacks,
            "unknown": unknown,
            "protected": protected,
        }


async def count_external_nonfollowers(username: str, max_scan: int = 200) -> dict:
    return await asyncio.to_thread(_count_external_nonfollowers_sync, username, max_scan)


def _run_unfollow_external_sync(username: str) -> dict:
    accounts_db = AccountsDB()
    db = DB()
    acc = accounts_db.get_account(username)
    if not acc:
        return {"ok": False, "error": "Conta nao encontrada"}

    with _account_guard(username) as acquired:
        if not acquired:
            return {"ok": False, "error": "Conta ocupada por outro job"}
        if risk_detector.is_paused(username):
            return {"ok": False, "error": "Conta pausada por risco"}

        remaining = max(
            0,
            int(acc.get("daily_unfollows", 40))
            - db.count_today_unfollows(acc["id"]),
        )
        if remaining <= 0:
            return {"ok": True, "removed": 0, "limit_reached": True}

        ig = _authenticate_account(acc, accounts_db)
        if not ig:
            return {"ok": False, "error": "Sessao invalida"}

        max_scan = min(500, max(100, remaining * 8))
        candidates = _following_candidates(ig, max_scan)
        unfollower = Unfollower(
            ig,
            risk_detector,
            WhitelistFilter(db.get_whitelist(acc["id"])),
        )

        def on_unfollow(uname, uid, kept: bool):
            if kept:
                return
            db.mark_unfollowed(acc["id"], uname)
            db.log_action(acc["id"], "unfollow", uname, "external_live_check", True)

        # keep_follow_backs faz uma segunda verificacao ao vivo imediatamente
        # antes de cada unfollow e falha fechado quando a API nao confirma.
        result = unfollower.unfollow_batch(
            candidates,
            daily_limit=remaining,
            delay_min=int(acc.get("delay_min", 30)),
            delay_max=int(acc.get("delay_max", 90)),
            on_success=on_unfollow,
            policy="keep_follow_backs",
        )
        _backup_current_session(accounts_db, ig)
        return {
            "ok": True,
            "removed": result["unfollowed"],
            "kept": result["kept"],
            "skipped": result["skipped"],
            "errors": result["errors"],
            "checked": len(candidates),
        }


async def run_unfollow_external_job(username: str):
    result = await asyncio.to_thread(_run_unfollow_external_sync, username)
    if not result.get("ok"):
        await _notify(
            f"❌ Unfollow externo de @{username} cancelado: {result.get('error', 'erro')}"
        )
        return result
    if result.get("limit_reached"):
        await _notify(f"ℹ️ @{username}: limite diario de unfollows ja atingido.")
        return result
    await _notify(
        f"✅ Unfollow externo — @{username}\n"
        f"Removidos: *{result.get('removed', 0)}* | "
        f"Mantidos: *{result.get('kept', 0)}* | "
        f"Nao confirmados: *{result.get('skipped', 0)}*"
    )
    return result


# ─── Scheduler ───────────────────────────────────────────────


def setup_scheduler(telegram_app=None) -> AsyncIOScheduler:
    global _telegram_app
    _telegram_app = telegram_app

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    risk_detector.set_notify_fn(_notify, loop=loop)
    risk_detector.set_persistence(
        load_fn=lambda username: BotStateDB().get_json(f"risk:{username}", {}),
        save_fn=lambda username, data: BotStateDB().set_json(
            f"risk:{username}", data
        ),
    )

    scheduler = AsyncIOScheduler(
        timezone="America/Sao_Paulo",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )

    # Follow: a cada 40 minutos; cada conta ainda respeita sua propria janela.
    scheduler.add_job(run_follow_job, CronTrigger(minute="*/40"), id="follow_job")
    scheduler.add_job(
        run_unfollow_job, CronTrigger(hour=9, minute=30), id="unfollow_job"
    )
    # Follow-backs sao checados uma vez ao dia, nao apos cada ciclo/manual.
    scheduler.add_job(
        run_auto_unfollow_follow_backs,
        CronTrigger(hour=18, minute=30),
        id="auto_unfollow_follow_backs",
    )
    scheduler.add_job(
        run_weekly_report,
        CronTrigger(day_of_week="sun", hour=20, minute=0),
        id="weekly_report",
    )
    scheduler.add_job(
        run_session_backup_job, CronTrigger(hour="*/6"), id="session_backup"
    )
    scheduler.add_job(
        run_warmup_job, CronTrigger(hour=23, minute=59), id="warmup_job"
    )
    scheduler.add_job(
        run_anomaly_check, CronTrigger(minute="*/30"), id="anomaly_check"
    )
    scheduler.add_job(
        run_daily_report_job, CronTrigger(hour=22, minute=15), id="daily_report"
    )
    return scheduler
