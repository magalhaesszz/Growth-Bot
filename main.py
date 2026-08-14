import logging
from telegram import Update
from telegram.ext import Application

from config import TELEGRAM_TOKEN, TELEGRAM_OWNER_ID, validate_config

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# O logger HTTP pode incluir a URL completa da Bot API, que contém o token.
# A biblioteca Telegram já encaminha falhas ao on_error sem expor essa URL.
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)


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
    except Exception as exc:
        logger.warning(
            "Não foi possível restaurar sessões (%s).",
            type(exc).__name__,
        )


async def post_init(app: Application):
    from scheduler.jobs import setup_scheduler

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
    logger.error("Erro não tratado (%s).", type(context.error).__name__)


def main():
    validate_config()
    from bot.handlers.contas import register_contas_handlers
    from bot.handlers.operacoes import register_operacoes_handlers
    from bot.handlers.video import register_video_handlers
    from bot.handlers.dashboard import register_dashboard_handlers

    logger.info("Iniciando Growth Bot...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_error_handler(on_error)

    # O dashboard registra /start e callbacks no grupo principal e deixa
    # texto/mídia guiados no grupo 1 para não interceptar conversas.
    register_dashboard_handlers(app)

    # ConversationHandlers em group=0 (padrão)
    register_contas_handlers(app)
    register_operacoes_handlers(app)
    register_video_handlers(app)


    logger.info("Bot rodando.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
