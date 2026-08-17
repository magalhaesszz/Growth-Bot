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
from instagram.risk_detector import risk_detector
from scheduler.warmup import advance_all_warmups, get_warmup_limit
from scheduler.anomaly import check_all_anomalies

logger = logging.getLogger(__name__)

# Referência ao app Telegram (injetada em setup_scheduler)
_telegram_app = None


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


LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _run_follow_job_sync(ignore_schedule: bool = False) -> list[str]:
    notifications: list[str] = []
    accounts_db = AccountsDB()
    db = DB()
    accounts = accounts_db.list_active_accounts()

    for acc in accounts:
        username = acc["username"]

        if risk_detector.is_paused(username):
            logger.warning(f"[{username}] Pausada — pulando follow job.")
            continue

        if not ignore_schedule:
            now_hour = datetime.now(LOCAL_TZ).hour
            if not (acc["hour_start"] <= now_hour < acc["hour_end"]):
                continue

        ig = InstagramClient(username, acc.get("password", ""), acc.get("fingerprint"))
        # Restaurar sessão salva — evita login com senha vazia
        from database.accounts import AccountsDB as _ADB
        _sess = _ADB().load_session_backup(username)
        if _sess:
            ig.load_session_from_data(_sess)
        elif not ig.is_logged_in():
            result = ig.login()
            if result != "ok":
                notifications.append(
                    f"❌ Falha de login — *@{username}* (`{result}`)."
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

        configured_limit = _get_daily_limit(acc)
        already_today = db.count_today_follows(acc["id"])
        daily_limit = max(0, configured_limit - already_today)
        if daily_limit <= 0 and not ignore_schedule:
            logger.info("[%s] Limite diário total já atingido.", username)
            continue
        elif daily_limit <= 0 and ignore_schedule:
            # Modo manual: reseta o limite parcial para continuar
            daily_limit = configured_limit
        campaign = db.get_active_campaign(acc["id"])
        campaign_id = campaign["id"] if campaign else None

        total_followed = 0

        for target in targets:
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

            remaining_limit = max(0, daily_limit - total_followed)
            if remaining_limit <= 0:
                break
            result = follower.follow_batch(
                profiles,
                daily_limit=remaining_limit,
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

    return notifications


async def run_follow_job(ignore_schedule: bool = False):
    notifications = await asyncio.to_thread(_run_follow_job_sync, ignore_schedule)
    # Auto-unfollow quem seguiu de volta
    await asyncio.to_thread(_auto_unfollow_follow_backs_sync)
    for message in notifications:
        await _notify(message)


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

        unfollower.unfollow_batch(
            candidates,
            daily_limit=max(
                0,
                acc.get("daily_unfollows", 40)
                - db.count_today_unfollows(acc["id"]),
            ),
            delay_min=acc.get("delay_min", 30),
            delay_max=acc.get("delay_max", 90),
            on_success=on_unfollow,
            policy=acc.get("unfollow_policy", "keep_follow_backs"),
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



# Modo manual
_MANUAL_MODE: bool = False
_MANUAL_TASK = None


def _auto_unfollow_follow_backs_sync():
    """Checa quem seguiu de volta e faz unfollow automatico."""
    try:
        from database.accounts import AccountsDB
        from database.operations import DB
        from instagram.client import InstagramClient
        from instagram.unfollower import Unfollower
        from instagram.score import WhitelistFilter

        adb = AccountsDB()
        db  = DB()
        accounts = adb.list_active_accounts()

        for acc in accounts:
            username = acc["username"]
            ig = InstagramClient(username, acc.get("password",""), acc.get("fingerprint"))
            sess = adb.load_session_backup(username)
            if sess:
                ig.load_session_from_data(sess)
            elif not ig.is_logged_in():
                continue

            wl = WhitelistFilter(db.get_whitelist(acc["id"]))
            unfollower = Unfollower(ig, risk_detector, wl)
            count = unfollower.auto_unfollow_follow_backs(
                acc["id"], db,
                daily_limit=acc.get("daily_unfollows", 50),
                delay_min=acc.get("delay_min", 30),
                delay_max=acc.get("delay_max", 90),
            )
            if count > 0:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _notify(f"🔄 @{username}: {count} auto-unfollows (seguiram de volta)"),
                    asyncio.get_event_loop()
                )
    except Exception as e:
        logger.error(f"Erro no auto_unfollow_follow_backs: {e}")


async def run_manual_mode():
    """Roda follow job continuamente até _MANUAL_MODE = False."""
    global _MANUAL_MODE
    logger.info("Modo manual iniciado.")
    while _MANUAL_MODE:
        await run_follow_job(ignore_schedule=True)
        if _MANUAL_MODE:
            import asyncio
            await asyncio.sleep(10)  # pausa minima entre ciclos
    logger.info("Modo manual encerrado.")


async def start_manual_mode(telegram_app=None) -> bool:
    """Liga o modo manual. Retorna False se já estiver rodando."""
    global _MANUAL_MODE, _MANUAL_TASK, _telegram_app
    if _MANUAL_MODE:
        return False
    if telegram_app:
        _telegram_app = telegram_app
    _MANUAL_MODE = True
    import asyncio
    _MANUAL_TASK = asyncio.create_task(run_manual_mode())
    return True


async def stop_manual_mode() -> bool:
    """Desliga o modo manual. Retorna False se não estiver rodando."""
    global _MANUAL_MODE, _MANUAL_TASK
    if not _MANUAL_MODE:
        return False
    _MANUAL_MODE = False
    if _MANUAL_TASK and not _MANUAL_TASK.done():
        _MANUAL_TASK.cancel()
    _MANUAL_TASK = None
    return True


def is_manual_mode() -> bool:
    return _MANUAL_MODE

def setup_scheduler(telegram_app=None) -> AsyncIOScheduler:
    global _telegram_app
    _telegram_app = telegram_app

    scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

    # Follow — a cada 2h dentro da janela operacional
    scheduler.add_job(run_follow_job, CronTrigger(minute="*/40"), id="follow_job")

    # Unfollow — 1x por dia às 9h30
    scheduler.add_job(run_unfollow_job, CronTrigger(hour=9, minute=30), id="unfollow_job")

    # Backup de sessões — a cada 6h
    scheduler.add_job(run_session_backup_job, CronTrigger(hour="*/6"), id="session_backup")

    # Aquecimento — avança dia às 23h59
    scheduler.add_job(run_warmup_job, CronTrigger(hour=23, minute=59), id="warmup_job")

    # Detector de anomalias — a cada 30 minutos
    scheduler.add_job(run_anomaly_check, CronTrigger(minute="*/30"), id="anomaly_check")

    # Relatório diário após o encerramento da janela padrão.
    scheduler.add_job(run_daily_report_job, CronTrigger(hour=22, minute=15), id="daily_report")

    return scheduler


async def run_unfollow_external_job(username: str):
    """
    Faz unfollow de contas que o usuario segue mas que nao seguem de volta.
    Funciona para follows feitos FORA do bot tambem.
    """
    import asyncio
    from database.accounts import AccountsDB
    from database.operations import DB
    from instagram.client import InstagramClient
    from instagram.unfollower import Unfollower
    from instagram.score import WhitelistFilter

    adb = AccountsDB()
    db  = DB()
    acc = adb.get_account(username)
    if not acc:
        return

    # Restaurar sessao
    ig = InstagramClient(username, acc.get("password", ""), acc.get("fingerprint"))
    sess = adb.load_session_backup(username)
    if sess:
        ig.load_session_from_data(sess)
    elif not ig.is_logged_in():
        await _notify(f"❌ Nao foi possivel autenticar @{username} para unfollow externo.")
        return

    # Buscar quem segue no Instagram via API
    try:
        following_raw = ig.api.user_following(ig.api.user_id)
        following_ids = {str(uid) for uid in following_raw.keys()}
    except Exception as e:
        await _notify(f"❌ Erro ao buscar following de @{username}: {e}")
        return

    # Buscar quem segue de volta
    follow_backs = {
        r["target_user_id"]
        for r in (db.sb.table("ig_followed")
            .select("target_user_id")
            .eq("account_id", acc["id"])
            .eq("follows_back", True)
            .execute().data or [])
    }

    # Quem nao segue de volta
    nao_seguem = following_ids - follow_backs
    if not nao_seguem:
        await _notify(f"✅ @{username} — nenhum nao-seguidor encontrado.")
        return

    await _notify(
        f"🔄 Iniciando unfollow de *{len(nao_seguem)}* nao-seguidores "
        f"de @{username}..."
    )

    wl = WhitelistFilter(db.get_whitelist(acc["id"]))
    unfollower = Unfollower(ig, risk_detector, wl)

    # Montar lista de candidatos no formato esperado
    candidates = [
        {"target_user_id": uid, "target_username": following_raw.get(int(uid), type("u", (), {"username": uid})()).username}
        for uid in nao_seguem
    ]

    count = 0
    for c in candidates:
        if risk_detector.is_paused(username):
            break
        try:
            ig.api.user_unfollow(c["target_user_id"])
            db.mark_unfollowed(acc["id"], c["target_username"])
            db.log_action(acc["id"], "unfollow", c["target_username"], success=True)
            count += 1
            import time, random
            time.sleep(random.uniform(
                acc.get("delay_min", 30),
                acc.get("delay_max", 90)
            ))
        except Exception as e:
            logger.error(f"[{username}] Erro unfollow externo {c['target_username']}: {e}")
            db.log_action(acc["id"], "unfollow", c["target_username"], success=False)

    await _notify(
        f"✅ Unfollow externo concluído — *{count}* pessoas removidas de @{username}."
    )
