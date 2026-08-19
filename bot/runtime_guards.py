import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.access import is_admin
from config import VIDEO_MAX_BATCH_MB, VIDEO_MAX_FILE_MB
from database.accounts import AccountsDB
from database.state import BotStateDB

logger = logging.getLogger(__name__)

# Comandos que alteram estado/conta ou executam automacao. Usuarios comuns
# continuam podendo consultar status, relatorios, logs e usar o editor de video.
_ADMIN_COMMANDS = [
    "alvo_add",
    "alvo_remover",
    "campanha_nova",
    "nicho_set",
    "score_set",
    "white_add",
    "black_add",
    "limite_set",
    "horario_set",
    "delay_set",
    "unfollow_prazo",
    "fila_limpar",
    "alerta_set",
    "sessao_backup",
    "sessao_restaurar",
    "pausar",
    "retomar",
    "deixar_seguir",
]

_ADMIN_DASH_PATTERN = (
    r"^dash:(?:"
    r"manual_start|manual_stop|"
    r"pause_all|resume_all|pause:|resume:|warmup:|"
    r"queue_clear|safe_mode(?:_off)?|"
    r"unfollow_naobot:(?:contar|executar)|"
    r"input:(?:add_target|new_campaign|add_white|add_black)|"
    r"cfg:|set_policy:|config_conta|cfg_conta:"
    r")"
)


def _is_admin_update(update: Update) -> bool:
    user = update.effective_user
    return bool(user and is_admin(user.id))


async def _guard_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _is_admin_update(update):
        return
    if update.message:
        await update.message.reply_text("⛔ Esta ação exige permissão de administrador.")
    raise ApplicationHandlerStop


async def _guard_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _is_admin_update(update):
        return
    query = update.callback_query
    if query:
        await query.answer("Ação restrita a administradores.", show_alert=True)
    raise ApplicationHandlerStop


async def _multi_account_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Corrige o painel de configuracao para usar a conta selecionada."""
    from bot.handlers import dashboard

    query = update.callback_query
    if query:
        await query.answer()
    data = query.data if query else ""
    acc = dashboard._selected_account(ctx)
    if not acc:
        await dashboard._show(update, "Nenhuma conta ativa.", dashboard._back_keyboard())
        raise ApplicationHandlerStop

    if data == "dash:config_conta":
        await dashboard._show(
            update,
            f"*Configurações de @{acc['username']}*\n\nToque em qualquer valor para editar:",
            dashboard._config_conta_keyboard(acc),
        )
        raise ApplicationHandlerStop

    if data in dashboard.CFG_LABELS:
        field, prompt = dashboard.CFG_LABELS[data]
        ctx.user_data["cfg_conta_field"] = field
        ctx.user_data["cfg_conta_username"] = acc["username"]
        dashboard._prompt(ctx, "cfg_conta_valor")
        await dashboard._show(
            update,
            f"*{prompt}*",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancelar", callback_data="dash:config_conta")]]
            ),
        )
        raise ApplicationHandlerStop


async def _external_unfollow_override(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Conta/execucao de unfollow externo sempre na conta selecionada e ao vivo."""
    from bot.handlers import dashboard
    from scheduler.jobs import count_external_nonfollowers, run_unfollow_external_job

    query = update.callback_query
    if query:
        await query.answer()
    acc = dashboard._selected_account(ctx)
    if not acc:
        await dashboard._show(update, "Nenhuma conta selecionada.", dashboard._back_keyboard())
        raise ApplicationHandlerStop

    data = query.data if query else ""
    if data.endswith(":contar"):
        await dashboard._show(
            update,
            f"⏳ Verificando relações ao vivo de @{acc['username']}...",
            dashboard._back_keyboard("dash:unfollow_naobot"),
        )
        result = await count_external_nonfollowers(acc["username"], max_scan=200)
        if not result.get("ok"):
            text = f"❌ Não foi possível verificar: {result.get('error', 'erro')}"
        else:
            text = (
                f"*Não-seguidores — @{acc['username']}*\n\n"
                f"Checados ao vivo: *{result.get('checked', 0)}*\n"
                f"Não seguem de volta: *{result.get('nonfollowers', 0)}*\n"
                f"Seguem de volta: *{result.get('followbacks', 0)}*\n"
                f"Whitelist: *{result.get('protected', 0)}*\n"
                f"Relação não confirmada: *{result.get('unknown', 0)}*\n\n"
                "Nenhum unfollow é feito quando a API não consegue confirmar a relação."
            )
        await dashboard._show(
            update,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Executar com verificação ao vivo",
                            callback_data="dash:unfollow_naobot:executar",
                        )
                    ],
                    [InlineKeyboardButton("Voltar", callback_data="dash:unfollow_naobot")],
                ]
            ),
        )
        raise ApplicationHandlerStop

    if data.endswith(":executar"):
        await dashboard._show(
            update,
            f"Iniciando limpeza segura de não-seguidores em *@{acc['username']}*.\n\n"
            "A relação será confirmada imediatamente antes de cada unfollow e o limite diário será respeitado.",
            dashboard._back_keyboard(),
        )
        asyncio.create_task(run_unfollow_external_job(acc["username"]))
        raise ApplicationHandlerStop


def _safe_key(username: str) -> str:
    return f"safe_mode:{username.casefold()}"


async def _safe_mode_override(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot.handlers import dashboard

    query = update.callback_query
    if query:
        await query.answer()
    acc = dashboard._selected_account(ctx)
    if not acc:
        await dashboard._show(update, "Nenhuma conta selecionada.", dashboard._back_keyboard())
        raise ApplicationHandlerStop

    state_db = BotStateDB()
    saved = state_db.get_json(_safe_key(acc["username"]), None)
    data = query.data if query else "dash:safety"

    if data == "dash:safety":
        label = "Desativar modo seguro" if saved else "Ativar modo seguro"
        callback = "dash:safe_mode_off" if saved else "dash:safe_mode"
        text = dashboard._safety_text(ctx)
        if saved:
            text += "\n\n🛡 *Modo seguro está ativo e pode ser revertido.*"
        await dashboard._show(
            update,
            text,
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(label, callback_data=callback)],
                    [InlineKeyboardButton("Voltar", callback_data="dash:home")],
                ]
            ),
        )
        raise ApplicationHandlerStop

    if data == "dash:safe_mode":
        if not saved:
            previous = {
                "daily_follows": int(acc.get("daily_follows", 40)),
                "daily_unfollows": int(acc.get("daily_unfollows", 40)),
                "delay_min": int(acc.get("delay_min", 30)),
                "delay_max": int(acc.get("delay_max", 90)),
            }
            if not state_db.set_json(_safe_key(acc["username"]), previous):
                await dashboard._show(
                    update,
                    "❌ Não ativei o modo seguro porque não foi possível salvar os valores anteriores para restauração.",
                    dashboard._back_keyboard(),
                )
                raise ApplicationHandlerStop
            safe_min = max(previous["delay_min"], 90)
            safe_max = max(previous["delay_max"], 180, safe_min)
            AccountsDB().update_settings(
                acc["username"],
                {
                    "daily_follows": min(previous["daily_follows"], 15),
                    "daily_unfollows": min(previous["daily_unfollows"], 15),
                    "delay_min": safe_min,
                    "delay_max": safe_max,
                },
            )
        await dashboard._show(
            update,
            "🛡 Modo seguro ativado. Os valores anteriores foram guardados para restauração.",
            dashboard._back_keyboard("dash:safety"),
        )
        raise ApplicationHandlerStop

    if data == "dash:safe_mode_off":
        if saved:
            AccountsDB().update_settings(acc["username"], saved)
            state_db.delete(_safe_key(acc["username"]))
        await dashboard._show(
            update,
            "✅ Modo seguro desativado e configurações anteriores restauradas.",
            dashboard._back_keyboard("dash:safety"),
        )
        raise ApplicationHandlerStop


async def _video_edit_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Completa os botoes Velocidade/Espelhar sem quebrar o ConversationHandler."""
    from bot.handlers.video import _menu_edicao_kb

    query = update.callback_query
    if not query:
        return
    await query.answer()
    raw = (query.data or "").split(":")
    action = raw[1] if len(raw) > 1 else ""
    video_id = raw[2] if len(raw) > 2 else ctx.user_data.get("dl_video_id", "")
    if video_id:
        ctx.user_data["dl_video_id"] = video_id
    edits = ctx.user_data.setdefault("dl_edits", {})

    if action == "speed":
        ctx.user_data["runtime_pending_speed"] = True
        await query.edit_message_text(
            "⏩ *Velocidade*\n\nDigite um valor entre `0.25` e `4.0` (ex.: `1.5`).",
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    if action == "flip":
        edits["flip"] = not bool(edits.get("flip", False))
        status = "ativado" if edits["flip"] else "desativado"
        await query.edit_message_text(
            f"🔄 Espelhamento *{status}*.",
            reply_markup=_menu_edicao_kb(edits, video_id),
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop


async def _video_speed_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("runtime_pending_speed"):
        return
    from bot.handlers.video import _menu_edicao_kb

    text = (update.message.text or "").strip().replace(",", ".")
    try:
        speed = float(text)
    except ValueError:
        await update.message.reply_text("❌ Digite um número, por exemplo `1.5`.", parse_mode="Markdown")
        raise ApplicationHandlerStop
    if not 0.25 <= speed <= 4.0:
        await update.message.reply_text("❌ Use um valor entre 0.25 e 4.0.")
        raise ApplicationHandlerStop

    ctx.user_data.pop("runtime_pending_speed", None)
    edits = ctx.user_data.setdefault("dl_edits", {})
    edits["speed"] = speed
    await update.message.reply_text(
        f"✅ Velocidade: *{speed:g}x*",
        reply_markup=_menu_edicao_kb(edits, ctx.user_data.get("dl_video_id", "")),
        parse_mode="Markdown",
    )
    raise ApplicationHandlerStop


async def _video_size_guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rejeita video grande antes de baixar os bytes para os 350 MB de RAM."""
    message = update.message
    if not message:
        return
    media = message.video or message.document
    file_size = int(getattr(media, "file_size", 0) or 0)
    if file_size <= 0:
        return

    max_file = VIDEO_MAX_FILE_MB * 1024 * 1024
    if file_size > max_file:
        await message.reply_text(
            f"❌ Arquivo de {file_size / 1048576:.1f} MB recusado. "
            f"Limite seguro: {VIDEO_MAX_FILE_MB} MB."
        )
        raise ApplicationHandlerStop

    existing = None
    if isinstance(ctx.user_data.get("video_lote"), list):
        existing = ctx.user_data["video_lote"]
    elif isinstance(ctx.user_data.get("lote_videos"), list):
        existing = ctx.user_data["lote_videos"]
    if existing is None:
        return

    current_bytes = sum(len(item[0]) for item in existing if item and isinstance(item[0], (bytes, bytearray)))
    if current_bytes + file_size > VIDEO_MAX_BATCH_MB * 1024 * 1024:
        await message.reply_text(
            f"❌ Este vídeo faria o lote ultrapassar {VIDEO_MAX_BATCH_MB} MB. "
            "Processe o lote atual antes de adicionar mais arquivos."
        )
        raise ApplicationHandlerStop


def register_runtime_guards(app) -> None:
    # Grupo -5: seguranca sempre antes de qualquer handler funcional.
    app.add_handler(CommandHandler(_ADMIN_COMMANDS, _guard_admin_command), group=-5)
    app.add_handler(
        CallbackQueryHandler(_guard_admin_callback, pattern=_ADMIN_DASH_PATTERN),
        group=-5,
    )
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, _video_size_guard),
        group=-5,
    )

    # Grupo -4: correcoes/overrides que substituem caminhos antigos do painel.
    app.add_handler(
        CallbackQueryHandler(
            _multi_account_config,
            pattern=r"^dash:(?:config_conta|cfg_conta:.*)$",
        ),
        group=-4,
    )
    app.add_handler(
        CallbackQueryHandler(
            _external_unfollow_override,
            pattern=r"^dash:unfollow_naobot:(?:contar|executar)$",
        ),
        group=-4,
    )
    app.add_handler(
        CallbackQueryHandler(
            _safe_mode_override,
            pattern=r"^dash:(?:safety|safe_mode|safe_mode_off)$",
        ),
        group=-4,
    )
    app.add_handler(
        CallbackQueryHandler(_video_edit_buttons, pattern=r"^dl:(?:speed|flip)(?::.*)?$"),
        group=-4,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _video_speed_text),
        group=-4,
    )
