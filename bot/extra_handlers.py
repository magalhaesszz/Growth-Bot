from telegram.ext import CallbackQueryHandler, CommandHandler

from bot.handlers.operacoes import (
    cmd_deixar_seguir,
    cmd_nao_seguem,
    cmd_seguidos,
    cmd_stats_completo,
    on_unfollow_batch,
)


def register_extra_handlers(app) -> None:
    """Registra comandos que ja existiam no modulo, mas estavam inacessiveis."""
    app.add_handler(CommandHandler("seguidos", cmd_seguidos))
    app.add_handler(CommandHandler("nao_seguem", cmd_nao_seguem))
    app.add_handler(CommandHandler("deixar_seguir", cmd_deixar_seguir))
    app.add_handler(CommandHandler("stats_completo", cmd_stats_completo))
    app.add_handler(
        CallbackQueryHandler(
            on_unfollow_batch,
            pattern=r"^(?:unfollow_batch:|unfollow_cancel$)",
        )
    )
