import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.access import is_admin
from database.state import BotStateDB

logger = logging.getLogger(__name__)

_STATE_KEY = "system_mode"
_NORMAL = "normal"
_VIDEO_ONLY = "video_only"
_cached_mode: str | None = None
_dashboard_patched = False
_risk_patched = False

_VIDEO_COMMANDS = {
    "/download",
    "/biblioteca",
    "/video_lote",
    "/processar_lote",
    "/fundo",
    "/fundos",
    "/video_status",
    "/config_video",
    "/config_video_reset",
    "/video_limpar",
}
_VIDEO_CONTEXT_COMMANDS = {
    "/download",
    "/biblioteca",
    "/video_lote",
    "/fundo",
    "/fundos",
}
_VIDEO_CALLBACK_PREFIXES = ("dash:video", "dl:", "lib:", "fnd:")
_VIDEO_CONTEXT_KEYS = {
    "dl_videos",
    "dl_video_id",
    "dl_video_bytes",
    "dl_edits",
    "dl_editor_token",
    "video_lote",
    "lote_videos",
    "dashboard_editor_source",
    "dashboard_editor_token",
}


def load_system_mode() -> str:
    """Carrega o modo persistido uma vez e mantém cache em memória."""
    global _cached_mode
    try:
        saved = BotStateDB().get(_STATE_KEY, _NORMAL)
    except Exception as exc:
        logger.warning("Falha ao carregar modo do sistema: %s", type(exc).__name__)
        saved = _NORMAL
    _cached_mode = _VIDEO_ONLY if saved == _VIDEO_ONLY else _NORMAL
    return _cached_mode


def is_video_only() -> bool:
    global _cached_mode
    if _cached_mode is None:
        load_system_mode()
    return _cached_mode == _VIDEO_ONLY


def _set_mode(mode: str) -> bool:
    global _cached_mode
    normalized = _VIDEO_ONLY if mode == _VIDEO_ONLY else _NORMAL
    _cached_mode = normalized
    try:
        return BotStateDB().set(_STATE_KEY, normalized)
    except Exception as exc:
        logger.warning("Falha ao persistir modo do sistema: %s", type(exc).__name__)
        return False


def _is_admin_update(update: Update) -> bool:
    user = update.effective_user
    return bool(user and is_admin(user.id))


def _restricted_keyboard(update: Update | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎬 Editor de Vídeo", callback_data="dash:video")],
    ]
    if update is None or _is_admin_update(update):
        rows.append(
            [
                InlineKeyboardButton(
                    "▶️ REATIVAR SISTEMA COMPLETO",
                    callback_data="system:resume",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔄 Atualizar", callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def _patch_dashboard() -> None:
    global _dashboard_patched
    if _dashboard_patched:
        return

    from bot.handlers import dashboard

    original_keyboard = dashboard._main_keyboard
    original_home_text = dashboard._home_text

    def patched_keyboard() -> InlineKeyboardMarkup:
        if is_video_only():
            return _restricted_keyboard()

        original = original_keyboard()
        rows = [list(row) for row in original.inline_keyboard]
        stop_row = [
            InlineKeyboardButton(
                "🛑 PARAR TUDO • SOMENTE VÍDEO",
                callback_data="system:video_only",
            )
        ]
        # Deixa o botão de emergência logo antes do botão final de atualizar.
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, stop_row)
        return InlineKeyboardMarkup(rows)

    def patched_home_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
        if is_video_only():
            return (
                "*Growth Bot — SOMENTE VÍDEO*\n\n"
                "🛑 Automações, modo manual e demais ações do bot estão parados.\n"
                "🎬 Somente o editor/processamento de vídeo permanece liberado.\n\n"
                "Use o botão abaixo para reativar o sistema completo."
            )
        return original_home_text(ctx)

    dashboard._main_keyboard = patched_keyboard
    dashboard._home_text = patched_home_text
    _dashboard_patched = True


def _patch_risk_guard() -> None:
    """Faz operações Instagram em andamento pararem no próximo checkpoint seguro."""
    global _risk_patched
    if _risk_patched:
        return

    from instagram.risk_detector import risk_detector

    original_is_paused = risk_detector.is_paused

    def guarded_is_paused(username: str) -> bool:
        if is_video_only():
            return True
        return original_is_paused(username)

    risk_detector.is_paused = guarded_is_paused
    _risk_patched = True


def _video_context(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if ctx.user_data.get("_video_only_context"):
        return True
    if any(key in ctx.user_data for key in _VIDEO_CONTEXT_KEYS):
        return True
    pending = ctx.user_data.get("dashboard_pending")
    return isinstance(pending, str) and pending.startswith("video_")


def _command_name(update: Update) -> str:
    message = update.effective_message
    if not message:
        return ""
    text = (message.text or message.caption or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


async def _system_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    if not _is_admin_update(update):
        await query.answer("Ação restrita a administradores.", show_alert=True)
        raise ApplicationHandlerStop

    await query.answer()
    data = query.data or ""

    if data == "system:video_only":
        # Cancela contexto manual do usuário que acionou o kill switch e impede
        # que o modo manual seja retomado depois de um restart.
        ctx.user_data.clear()
        persisted = _set_mode(_VIDEO_ONLY)
        try:
            BotStateDB().set("manual_mode", "false")
        except Exception:
            pass

        try:
            from scheduler.jobs import is_manual_mode, stop_manual_mode

            if is_manual_mode():
                await stop_manual_mode()
        except Exception as exc:
            logger.warning("Falha ao encerrar modo manual: %s", type(exc).__name__)

        scheduler = ctx.application.bot_data.get("scheduler")
        if scheduler:
            try:
                scheduler.pause()
            except Exception as exc:
                logger.warning("Falha ao pausar scheduler: %s", type(exc).__name__)

        suffix = "" if persisted else "\n\n⚠️ O modo está ativo neste processo, mas a persistência falhou."
        await query.edit_message_text(
            "🛑 *SISTEMA PARADO — SOMENTE VÍDEO*\n\n"
            "• Modo manual: parado\n"
            "• Agendador/automação: pausado\n"
            "• Ações Instagram: bloqueadas\n"
            "• Comandos e botões não relacionados a vídeo: bloqueados\n"
            "• Editor/processamento de vídeo: liberado"
            + suffix,
            reply_markup=_restricted_keyboard(update),
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    if data == "system:resume":
        persisted = _set_mode(_NORMAL)
        ctx.user_data.clear()
        scheduler = ctx.application.bot_data.get("scheduler")
        if scheduler:
            try:
                scheduler.resume()
            except Exception as exc:
                logger.warning("Falha ao retomar scheduler: %s", type(exc).__name__)

        from bot.handlers import dashboard

        suffix = "" if persisted else "\n\n⚠️ A reativação não pôde ser persistida."
        await query.edit_message_text(
            "✅ *SISTEMA COMPLETO REATIVADO*\n\n"
            "O agendador voltou a funcionar. O modo manual continua desligado e só inicia novamente quando você mandar."
            + suffix,
            reply_markup=dashboard._main_keyboard(),
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop


async def _callback_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_video_only():
        return

    query = update.callback_query
    if not query:
        return
    data = query.data or ""

    if data == "dash:home":
        return
    if data.startswith(_VIDEO_CALLBACK_PREFIXES):
        ctx.user_data["_video_only_context"] = True
        return

    await query.answer(
        "Sistema parado. Somente a edição de vídeo está liberada.",
        show_alert=True,
    )
    raise ApplicationHandlerStop


async def _message_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_video_only():
        return

    message = update.effective_message
    if not message:
        return

    command = _command_name(update)
    if command == "/start":
        return

    if command in _VIDEO_COMMANDS:
        if command in _VIDEO_CONTEXT_COMMANDS:
            ctx.user_data["_video_only_context"] = True
        return

    if command in {"/cancelar", "/pular"} and _video_context(ctx):
        return

    if not command and _video_context(ctx):
        return

    await message.reply_text(
        "🛑 *Sistema em modo SOMENTE VÍDEO.*\n\n"
        "Automações e comandos manuais estão bloqueados. Use o editor de vídeo ou reative o sistema completo.",
        reply_markup=_restricted_keyboard(update),
        parse_mode="Markdown",
    )
    raise ApplicationHandlerStop


def apply_startup_mode(app, scheduler) -> bool:
    """Aplica o modo persistido durante o boot antes de retomar automações."""
    active = load_system_mode() == _VIDEO_ONLY
    if active:
        try:
            scheduler.pause()
        except Exception as exc:
            logger.warning("Falha ao restaurar scheduler pausado: %s", type(exc).__name__)
        try:
            BotStateDB().set("manual_mode", "false")
        except Exception:
            pass
        logger.warning("Modo SOMENTE VÍDEO restaurado do estado persistido.")
    return active


def register_video_only_mode(app) -> None:
    _patch_dashboard()
    _patch_risk_guard()

    # Grupos bem anteriores aos demais guards/handlers: o kill switch precisa
    # bloquear qualquer fluxo não-vídeo antes que ele execute.
    app.add_handler(
        CallbackQueryHandler(
            _system_toggle,
            pattern=r"^system:(?:video_only|resume)$",
        ),
        group=-20,
    )
    app.add_handler(CallbackQueryHandler(_callback_gate), group=-19)
    app.add_handler(MessageHandler(filters.ALL, _message_gate), group=-19)
