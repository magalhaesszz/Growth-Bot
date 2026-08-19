from telegram.ext import CallbackQueryHandler, CommandHandler

from bot.access import is_admin
from bot.handlers.operacoes import (
    cmd_deixar_seguir,
    cmd_nao_seguem,
    cmd_seguidos,
    cmd_stats_completo,
    on_unfollow_batch,
)


async def _admin_unfollow_batch(update, ctx):
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer(
                "Ação restrita a administradores.", show_alert=True
            )
        return
    return await on_unfollow_batch(update, ctx)


def register_extra_handlers(app) -> None:
    """Registra comandos que ja existiam no modulo, mas estavam inacessiveis."""
    app.add_handler(CommandHandler("seguidos", cmd_seguidos))
    app.add_handler(CommandHandler("nao_seguem", cmd_nao_seguem))
    app.add_handler(CommandHandler("deixar_seguir", cmd_deixar_seguir))
    app.add_handler(CommandHandler("stats_completo", cmd_stats_completo))
    app.add_handler(
        CallbackQueryHandler(
            _admin_unfollow_batch,
            pattern=r"^(?:unfollow_batch:|unfollow_cancel$)",
        )
    )
