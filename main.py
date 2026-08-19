import logging

from telegram.ext import Application

from bot.extra_handlers import register_extra_handlers
from bot.handlers.contas import register_contas_handlers
from bot.handlers.dashboard import register_dashboard_handlers
from bot.handlers.operacoes import register_operacoes_handlers
from bot.handlers.video import register_video_handlers
from bot.runtime_guards import register_runtime_guards
from config import TELEGRAM_TOKEN, validate_config
from scheduler.jobs import setup_scheduler
from scheduler.story_monitor import attach_story_monitor

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _restore_sessions():
    try:
        from database.accounts import AccountsDB
        from instagram.client import InstagramClient

        accounts_db = AccountsDB()
        restored = 0
        for acc in accounts_db.list_active_accounts():
            data = accounts_db.load_session_backup(acc["username"])
            if not data:
                continue
            ig = InstagramClient(
                acc["username"], acc.get("password", ""), acc.get("fingerprint")
            )
            ig.load_session_from_data(data)
            restored += 1
        if restored:
            logger.info("Sessoes restauradas do Supabase: %s conta(s).", restored)
    except Exception as exc:
        logger.warning("Nao foi possivel restaurar sessoes: %s", exc)


async def post_init(app: Application):
    await _restore_sessions()
    scheduler = setup_scheduler(telegram_app=app)
    attach_story_monitor(scheduler)
    scheduler.start()
    # Mantem referencia para um shutdown limpo e evita GC acidental.
    app.bot_data["scheduler"] = scheduler
    logger.info("Agendador iniciado.")

    from scheduler.jobs import resume_manual_mode_if_needed

    await resume_manual_mode_if_needed(telegram_app=app)


async def post_shutdown(app: Application):
    try:
        from scheduler.jobs import is_manual_mode, stop_manual_mode

        if is_manual_mode():
            await stop_manual_mode()
    except Exception as exc:
        logger.warning("Erro ao encerrar modo manual: %s", exc)

    scheduler = app.bot_data.get("scheduler")
    if scheduler and getattr(scheduler, "running", False):
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Erro ao encerrar scheduler: %s", exc)


async def on_error(update: object, context):
    logger.error("Erro nao tratado: %s", context.error, exc_info=context.error)


def main():
    validate_config()
    logger.info("Iniciando Growth Bot...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_error_handler(on_error)

    # Grupos -5/-4: permissao, limites de memoria e overrides de seguranca.
    register_runtime_guards(app)

    # Dashboard em -1; conversas/comandos funcionais ficam nos grupos padrao.
    register_dashboard_handlers(app)
    register_contas_handlers(app)
    register_operacoes_handlers(app)
    register_extra_handlers(app)
    register_video_handlers(app)

    logger.info("Bot rodando.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
