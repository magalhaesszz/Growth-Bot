import logging

from telegram.ext import Application

from bot.extra_handlers import register_extra_handlers
from bot.handlers.contas import register_contas_handlers
from bot.handlers.dashboard import register_dashboard_handlers
from bot.handlers.operacoes import register_operacoes_handlers
from bot.handlers.video_bulk import register_video_handlers
from bot.runtime_guards import register_runtime_guards
from bot.video_only_mode import apply_startup_mode, is_video_only, register_video_only_mode
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
    # O kill switch persiste no Supabase. Quando o processo volta em modo
    # somente-video, nem as sessoes Instagram sao restauradas.
    video_only = is_video_only()
    if not video_only:
        await _restore_sessions()
    else:
        logger.warning("Boot em modo SOMENTE VIDEO; restauracao Instagram ignorada.")

    scheduler = setup_scheduler(telegram_app=app)
    attach_story_monitor(scheduler)
    scheduler.start()
    # Mantem referencia para controle pelo botao e para um shutdown limpo.
    app.bot_data["scheduler"] = scheduler

    if apply_startup_mode(app, scheduler):
        video_only = True
        logger.warning("Agendador iniciado e imediatamente pausado pelo kill switch.")
    else:
        video_only = False
        logger.info("Agendador iniciado.")

    if not video_only:
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

    # Kill switch em -20/-19: precisa rodar antes de qualquer acao funcional.
    register_video_only_mode(app)

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
