import logging
from telegram import Update
from telegram.ext import Application

from config import TELEGRAM_TOKEN, TELEGRAM_OWNER_ID
from bot.handlers.contas import register_contas_handlers
from bot.handlers.operacoes import register_operacoes_handlers
from bot.handlers.video import register_video_handlers
from bot.handlers.dashboard import register_dashboard_handlers
from scheduler.jobs import setup_scheduler

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
        accounts = accounts_db.list_active_accounts()
        restored = 0
        for acc in accounts:
            data = accounts_db.load_session_backup(acc["username"])
            if data:
                ig = InstagramClient(acc["username"], acc["password"], acc.get("fingerprint"))
                ig.load_session_from_data(data)
                restored += 1
        if restored:
            logger.info(f"Sessões restauradas do Supabase: {restored} conta(s).")
    except Exception as e:
        logger.warning(f"Não foi possível restaurar sessões: {e}")


async def post_init(app: Application):
    await _restore_sessions()
    scheduler = setup_scheduler(telegram_app=app)
    scheduler.start()
    logger.info("Agendador iniciado.")
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_OWNER_ID,
            text=(
                "✅ *Growth Bot online!*\n"
                "Use /start para abrir o painel de controle."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def on_error(update: object, context):
    logger.error(f"Erro não tratado: {context.error}", exc_info=context.error)


def main():
    logger.info("Iniciando Growth Bot...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_error_handler(on_error)

    # Dashboard em group=-1 (prioridade máxima)
    # /start e dash:callbacks respondem ANTES de qualquer ConversationHandler
    register_dashboard_handlers(app)

    # ConversationHandlers em group=0 (padrão)
    register_contas_handlers(app)
    register_operacoes_handlers(app)
    register_video_handlers(app)

    # /start também como fallback em group=1 para garantir que
    # usuarios de fora não fiquem presos em ConversationHandlers
    from telegram.ext import CommandHandler as CmdH
    from bot.handlers.dashboard import cmd_start
    app.add_handler(CmdH("start", cmd_start), group=1)

    logger.info("Bot rodando.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
