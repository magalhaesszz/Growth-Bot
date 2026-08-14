import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import WARMUP_SCHEDULE, TELEGRAM_OWNER_ID
from database.accounts import AccountsDB
from database.operations import DB
from instagram.client import InstagramClient
from instagram.scraper import Scraper
from instagram.follower import Follower
from instagram.unfollower import Unfollower
from instagram.stories import StoriesViewer
from instagram.score import ProfileScorer, BlacklistFilter, WhitelistFilter
from instagram.risk_detector import get_risk_detector
from scheduler.warmup import advance_all_warmups, get_warmup_limit
from scheduler.anomaly import check_all_anomalies

logger = logging.getLogger(__name__)

# Instância global compartilhada entre todos os jobs
risk_detector = get_risk_detector()

# Referência ao app Telegram (injetada em setup_scheduler)
_telegram_app = None
_event_loop = None


async def _notify(message: str):
    """Envia mensagem ao dono via Telegram."""
    if _telegram_app:
        try:
            await _telegram_app.bot.send_message(
                chat_id=TELEGRAM_OWNER_ID,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Erro ao enviar notificação Telegram: {e}")


def _get_daily_limit(account: dict) -> int:
    warmup_day = account.get("warmup_day", 0)
    if warmup_day > 0:
        return get_warmup_limit(warmup_day)
    return account.get("daily_follows", 40)


def _run_follow_job_sync():
    accounts_db = AccountsDB()
    db = DB()
    accounts = accounts_db.list_active_accounts()

    for acc in accounts:
        username = acc["username"]

        if risk_detector.is_paused(username):
            logger.warning(f"[{username}] Pausada — pulando follow job.")
            continue

        now_hour = datetime.now(ZoneInfo("America/Sao_Paulo")).hour
        if not (acc["hour_start"] <= now_hour < acc["hour_end"]):
            continue

        ig = InstagramClient(username, acc["password"], acc.get("fingerprint"))
        result = ig.login()
        if result != "ok":
            if _event_loop:
                asyncio.run_coroutine_threadsafe(
                    _notify(f"❌ Falha de login — *@{username}*. Verifique as credenciais."),
                    _event_loop,
                )
            continue

        targets = db.list_targets(acc["id"])
        if not targets:
            logger.info(f"[{username}] Sem alvos cadastrados.")
            continue

        already_following = db.get_already_following_ids(acc["id"])
        bl_filter = BlacklistFilter(db.get_blacklist(acc["id"]))
        scorer = ProfileScorer()
        follower = Follower(ig, risk_detector, scorer, bl_filter)

        daily_limit = max(0, _get_daily_limit(acc) - db.count_today_follows(acc["id"]))
        if daily_limit == 0:
            logger.info("[%s] Limite diário de follows já atingido.", username)
            continue
        campaign = db.get_active_campaign(acc["id"])
        campaign_id = campaign["id"] if campaign else None

        total_followed = 0

        for target in targets:
            remaining = daily_limit - total_followed
            if remaining <= 0:
                break
            scraper = Scraper(ig)

            if not target.get("page_user_id"):
                page = scraper.resolve_page(target["page_url"])
                if not page:
                    continue
                db.sb.table("ig_targets").update({
                    "page_username": page["username"],
                    "page_user_id": page["user_id"],
                }).eq("id", target["id"]).execute()
                target.update(page)

            profiles = scraper.get_followers(
                target["page_user_id"],
                target["page_username"],
                limit=150,
                already_following=already_following,
            )

            def on_follow(uname, uid, _campaign_id=campaign_id, _acc=acc):
                db.add_followed(_acc["id"], uid, uname, _campaign_id)
                already_following.add(str(uid))
                db.log_action(_acc["id"], "follow", uname, success=True)
                if _campaign_id:
                    db.update_campaign_stats(_campaign_id, follows=1)
                accounts_db.update_last_active(_acc["username"])

            result = follower.follow_batch(
                profiles,
                daily_limit=daily_limit,
                min_score=acc.get("score_min", 50),
                delay_min=acc.get("delay_min", 30),
                delay_max=acc.get("delay_max", 90),
                on_success=on_follow,
            )
            total_followed += result["followed"]

            db.update_target_scraped(target["id"], result["followed"])

            if risk_detector.is_paused(username):
                break

        # Stories nos perfis seguidos recentemente
        if not risk_detector.is_paused(username):
            recent_ids = list(db.get_already_following_ids(acc["id"]))[:30]
            viewer = StoriesViewer(ig, risk_detector)
            story_result = viewer.view_stories_for_users(
                recent_ids, max_per_run=20, delay_min=5, delay_max=15
            )
            db.log_action(acc["id"], "story_view", detail=f"Vistos: {story_result['viewed']}")

        logger.info(f"[{username}] Follow job finalizado — {total_followed} follows.")


async def run_follow_job():
    await asyncio.to_thread(_run_follow_job_sync)


def _run_unfollow_job_sync():
    accounts_db = AccountsDB()
    db = DB()
    accounts = accounts_db.list_active_accounts()

    for acc in accounts:
        username = acc["username"]

        if risk_detector.is_paused(username):
            continue

        candidates = db.get_unfollow_candidates(acc["id"], acc.get("unfollow_after_days", 5))
        if not candidates:
            continue

        ig = InstagramClient(username, acc["password"], acc.get("fingerprint"))
        result = ig.login()
        if result != "ok":
            continue

        wl = WhitelistFilter(db.get_whitelist(acc["id"]))
        unfollower = Unfollower(ig, risk_detector, wl)

        campaign = db.get_active_campaign(acc["id"])
        campaign_id = campaign["id"] if campaign else None

        def on_unfollow(uname, uid, kept: bool, _acc=acc, _campaign_id=campaign_id):
            if kept:
                db.mark_follows_back(_acc["id"], uname)
                db.log_action(_acc["id"], "follow_back_detected", uname)
                if _campaign_id:
                    db.update_campaign_stats(_campaign_id, follow_backs=1)
            else:
                db.mark_unfollowed(_acc["id"], uname)
                db.log_action(_acc["id"], "unfollow", uname)
                if _campaign_id:
                    db.update_campaign_stats(_campaign_id, unfollows=1)

        remaining = max(
            0,
            acc.get("daily_unfollows", 40)
            - db.count_today_actions(acc["id"], "unfollow"),
        )
        if remaining == 0:
            continue

        unfollower.unfollow_batch(
            candidates,
            daily_limit=remaining,
            delay_min=acc.get("delay_min", 30),
            delay_max=acc.get("delay_max", 90),
            policy=acc.get("unfollow_policy", "keep_follow_backs"),
            on_success=on_unfollow,
        )


async def run_unfollow_job():
    await asyncio.to_thread(_run_unfollow_job_sync)


def _run_session_backup_job_sync():
    accounts_db = AccountsDB()
    for acc in accounts_db.list_active_accounts():
        ig = InstagramClient(acc["username"], acc["password"], acc.get("fingerprint"))
        data = ig.get_session_data()
        if data:
            accounts_db.save_session_backup(acc["username"], data)
    logger.info("Backup de sessões concluído.")


async def run_session_backup_job():
    await asyncio.to_thread(_run_session_backup_job_sync)


async def run_warmup_job():
    await advance_all_warmups(notify_fn=_notify)


async def run_anomaly_check():
    await check_all_anomalies(risk_detector, notify_fn=_notify)


def _build_reports_sync() -> list[tuple[str, object]]:
    from reports.daily import ReportGenerator

    generator = ReportGenerator()
    reports = []
    for acc in AccountsDB().list_active_accounts():
        if not acc.get("daily_report_enabled", True):
            continue
        reports.append((
            generator.generate_text(acc["id"], acc["username"]),
            generator.generate_chart(acc["id"], acc["username"]),
        ))
    return reports


async def run_daily_report_job():
    if not _telegram_app:
        return
    try:
        reports = await asyncio.to_thread(_build_reports_sync)
        for report_text, chart in reports:
            await _telegram_app.bot.send_message(
                chat_id=TELEGRAM_OWNER_ID,
                text=report_text,
                parse_mode="Markdown",
            )
            await _telegram_app.bot.send_photo(
                chat_id=TELEGRAM_OWNER_ID,
                photo=chart,
            )
    except Exception as exc:
        logger.error("Falha no relatório automático: %s", type(exc).__name__)


def setup_scheduler(telegram_app=None) -> AsyncIOScheduler:
    global _telegram_app, _event_loop
    _telegram_app = telegram_app
    _event_loop = asyncio.get_running_loop()

    scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

    # Follow — a cada 2h dentro da janela operacional
    scheduler.add_job(run_follow_job, CronTrigger(hour="8,10,12,14,16,18,20"), id="follow_job")

    # Unfollow — 1x por dia às 9h30
    scheduler.add_job(run_unfollow_job, CronTrigger(hour=9, minute=30), id="unfollow_job")

    # Backup de sessões — a cada 6h
    scheduler.add_job(run_session_backup_job, CronTrigger(hour="*/6"), id="session_backup")

    # Aquecimento — avança dia às 23h59
    scheduler.add_job(run_warmup_job, CronTrigger(hour=23, minute=59), id="warmup_job")

    # Detector de anomalias — a cada 30 minutos
    scheduler.add_job(run_anomaly_check, CronTrigger(minute="*/30"), id="anomaly_check")

    # Relatório dos últimos 7 dias, enviado diariamente às 21h30.
    scheduler.add_job(run_daily_report_job, CronTrigger(hour=21, minute=30), id="daily_report")

    return scheduler
