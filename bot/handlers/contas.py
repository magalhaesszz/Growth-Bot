import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, CommandHandler,
    MessageHandler, ConversationHandler, filters,
)

from database.accounts import AccountsDB
from instagram.client import (
    InstagramClient, PENDING_CHALLENGES,
    detect_code_type, format_preview,
)
from config import TELEGRAM_OWNER_ID, WARMUP_SCHEDULE

logger = logging.getLogger(__name__)
accounts_db = AccountsDB()

AGUARDANDO_METODO  = 0
AGUARDANDO_CODIGO  = 1
AGUARDANDO_CONFIRM = 2
AGUARDANDO_2FA     = 3


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            if update.message:
                await update.message.reply_text("⛔ Acesso negado.")
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ─── /conta_add ──────────────────────────────────────────────

async def cmd_conta_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("⛔ Acesso negado.")
        return ConversationHandler.END

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Uso: `/conta_add @usuario senha`", parse_mode="Markdown")
        return ConversationHandler.END

    username = ctx.args[0].lstrip("@")
    password = ctx.args[1]
    ctx.user_data["challenge_username"] = username
    ctx.user_data["challenge_password"] = password

    await update.message.reply_text(
        f"🔄 Conectando *@{username}*...", parse_mode="Markdown")

    import asyncio
    ig = InstagramClient(username, password)
    result = await asyncio.get_event_loop().run_in_executor(None, ig.login)

    if result == "ok":
        _salvar_conta(ig, username, password)
        schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
        await update.message.reply_text(
            f"✅ *@{username}* conectada!\n"
            f"🔥 Aquecimento: {schedule_str} follows/dia",
            parse_mode="Markdown")
        return ConversationHandler.END

    elif result == "challenge":
        ctx.user_data["challenge_ig"] = ig
        return await _mostrar_selecao_metodo(update, username)

    elif result == "two_factor":
        ctx.user_data["challenge_ig"] = ig
        await update.message.reply_text(
            f"🔐 *2FA ativo — @{username}*\n\n"
            f"Digite o código do autenticador (6 dígitos):",
            parse_mode="Markdown")
        return AGUARDANDO_2FA

    elif result in ("error:rate_limit", "error:rate_limit_429"):
        await update.message.reply_text(
            f"🚫 *Rate limit — @{username}*\n\n"
            f"Instagram bloqueou o IP temporariamente.\n"
            f"⏳ Aguarde *30-60 minutos* antes de tentar.",
            parse_mode="Markdown")
        return ConversationHandler.END

    elif result == "error:bad_password":
        await update.message.reply_text(
            f"❌ *Senha incorreta* para @{username}.",
            parse_mode="Markdown")
        return ConversationHandler.END

    elif result == "error:feedback_required":
        await update.message.reply_text(
            f"⚠️ Instagram bloqueou *@{username}* temporariamente.\n"
            f"Acesse o app pelo celular e confirme sua identidade.",
            parse_mode="Markdown")
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            f"❌ Erro: `{result}`", parse_mode="Markdown")
        return ConversationHandler.END


async def _mostrar_selecao_metodo(update, username):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Email",  callback_data="verify:email"),
         InlineKeyboardButton("📱 SMS",    callback_data="verify:sms")],
        [InlineKeyboardButton("🔑 Código de backup (8 dígitos)", callback_data="verify:backup")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="verify:cancel")],
    ])
    await update.message.reply_text(
        f"📱 *Verificação — @{username}*\n\n"
        f"Escolha como receber o código:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_METODO


# ─── Seleção de método ───────────────────────────────────────

async def escolher_metodo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    data     = query.data.replace("verify:", "")
    username = ctx.user_data.get("challenge_username", "")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")

    if data == "cancel" or not ig:
        PENDING_CHALLENGES.pop(username, None)
        await query.edit_message_text("❌ Conexão cancelada.")
        return ConversationHandler.END

    if data == "backup":
        ctx.user_data["verify_type"] = "backup"
        await query.edit_message_text(
            f"🔑 *Código de backup — @{username}*\n\n"
            f"Digite o código de *8 dígitos* (com ou sem hífen):\n"
            f"Exemplo: `12345678` ou `1234-5678`\n\n"
            f"_Use /cancelar para cancelar._",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CODIGO

    import asyncio
    label = "e-mail" if data == "email" else "SMS"
    await query.edit_message_text(
        f"⏳ Solicitando código via *{label}*...", parse_mode="Markdown")

    result = await asyncio.get_event_loop().run_in_executor(
        None, ig.start_challenge_with_method, data)

    ctx.user_data["verify_type"] = data
    await query.edit_message_text(
        f"📨 Código enviado via *{label}*!\n\n"
        f"✏️ Digite o código de *6 dígitos*:\n"
        f"Exemplo: `123456` ou `123-456`\n\n"
        f"_Use /cancelar para cancelar._",
        parse_mode="Markdown"
    )
    return AGUARDANDO_CODIGO


# ─── Receber código — com preview e detecção automática ──────

async def receber_codigo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    code_raw  = update.message.text.strip()
    username  = ctx.user_data.get("challenge_username", "")
    password  = ctx.user_data.get("challenge_password", "")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")
    verify_type = ctx.user_data.get("verify_type", "email")

    if not ig:
        await update.message.reply_text(
            "❌ Sessão expirada. Use `/conta_add` novamente.",
            parse_mode="Markdown")
        return ConversationHandler.END

    # Normalizar e detectar tipo automaticamente
    import re as _re
    clean = _re.sub(r"[\s\-]+", "", code_raw)

    if not clean.isdigit():
        await update.message.reply_text(
            "❌ Código inválido — digite apenas números (com ou sem hífen).")
        return AGUARDANDO_CODIGO

    tipo_detectado = detect_code_type(clean)
    preview = format_preview(clean)

    # Validar tamanho
    if len(clean) == 6:
        tipo_label = "SMS / Email"
        emoji = "📨"
    elif len(clean) == 8:
        tipo_label = "Código de backup"
        emoji = "🔑"
    elif len(clean) == 6 and verify_type == "backup":
        await update.message.reply_text(
            "❌ Backup code tem 8 dígitos. Você digitou 6.\n"
            "Digite os 8 dígitos completos.")
        return AGUARDANDO_CODIGO
    else:
        await update.message.reply_text(
            f"❌ Código com {len(clean)} dígitos não reconhecido.\n"
            f"• 6 dígitos = SMS ou Email\n"
            f"• 8 dígitos = Código de backup")
        return AGUARDANDO_CODIGO

    # Mostrar preview para confirmação
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar e enviar", callback_data=f"code:confirm:{clean}"),
         InlineKeyboardButton("✏️ Digitar de novo", callback_data="code:retype")],
    ])
    ctx.user_data["code_pending"] = clean
    ctx.user_data["code_type"] = tipo_detectado

    await update.message.reply_text(
        f"{emoji} *Confirmar envio?*\n\n"
        f"Código: `{preview}`\n"
        f"Tipo: *{tipo_label}*\n"
        f"Conta: *@{username}*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_CONFIRM


# ─── Confirmação do código ───────────────────────────────────

async def confirmar_codigo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    data = query.data

    if data == "code:retype":
        verify_type = ctx.user_data.get("verify_type", "email")
        tipo_label = "8 dígitos (backup)" if verify_type == "backup" else "6 dígitos"
        await query.edit_message_text(
            f"✏️ Digite o código novamente ({tipo_label}):")
        return AGUARDANDO_CODIGO

    if not data.startswith("code:confirm:"):
        return AGUARDANDO_CONFIRM

    clean      = data.replace("code:confirm:", "")
    username   = ctx.user_data.get("challenge_username", "")
    password   = ctx.user_data.get("challenge_password", "")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")
    tipo       = ctx.user_data.get("code_type", "sms_or_totp")

    await query.edit_message_text(
        f"⏳ Enviando código `{format_preview(clean)}` para @{username}...",
        parse_mode="Markdown")

    import asyncio

    if tipo == "backup" or len(clean) == 8:
        sucesso = await asyncio.get_event_loop().run_in_executor(
            None, ig.submit_backup_code, clean)
    else:
        result = ig.submit_code(clean)
        if result == "ok":
            sucesso = True
        elif result == "pending":
            await asyncio.sleep(5)
            sucesso = ig.is_logged_in()
        else:
            # Tentar login direto
            try:
                sucesso = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ig.cl.login(username, password, verification_code=clean)
                )
                if sucesso:
                    ig._save_session()
            except Exception as e:
                logger.error(f"Erro ao confirmar código: {e}")
                sucesso = False

    if sucesso:
        _salvar_conta(ig, username, password)
        schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
        await query.message.reply_text(
            f"✅ *@{username}* verificada e conectada!\n"
            f"🔥 Aquecimento: {schedule_str} follows/dia",
            parse_mode="Markdown")
    else:
        await query.message.reply_text(
            f"❌ Código inválido ou expirado para *@{username}*.\n"
            f"Use `/conta_add @{username} {password}` para tentar novamente.",
            parse_mode="Markdown")

    ctx.user_data.clear()
    return ConversationHandler.END


# ─── 2FA ─────────────────────────────────────────────────────

async def receber_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    code_raw = update.message.text.strip()
    username = ctx.user_data.get("challenge_username", "")
    password = ctx.user_data.get("challenge_password", "")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")

    if not ig:
        await update.message.reply_text("❌ Sessão expirada.")
        return ConversationHandler.END

    import re as _re
    clean = _re.sub(r"[\s\-]+", "", code_raw)
    preview = format_preview(clean)

    # Preview antes de enviar
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar", callback_data=f"2fa:confirm:{clean}"),
         InlineKeyboardButton("✏️ Redigitar", callback_data="2fa:retype")],
    ])
    await update.message.reply_text(
        f"🔐 *Confirmar código 2FA?*\n\n"
        f"Código: `{preview}`\n"
        f"Conta: *@{username}*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_CONFIRM


async def confirmar_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "2fa:retype":
        await query.edit_message_text("✏️ Digite o código 2FA novamente:")
        return AGUARDANDO_2FA

    if not data.startswith("2fa:confirm:"):
        return AGUARDANDO_CONFIRM

    clean    = data.replace("2fa:confirm:", "")
    username = ctx.user_data.get("challenge_username", "")
    password = ctx.user_data.get("challenge_password", "")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")

    import asyncio
    await query.edit_message_text(f"⏳ Verificando 2FA para *@{username}*...", parse_mode="Markdown")
    sucesso = await asyncio.get_event_loop().run_in_executor(None, ig.submit_2fa, clean)

    if sucesso:
        _salvar_conta(ig, username, password)
        await query.message.reply_text(f"✅ *@{username}* com 2FA conectada!", parse_mode="Markdown")
    else:
        await query.message.reply_text(
            f"❌ Código 2FA incorreto.\n"
            f"Use `/conta_add @{username} {password}` para tentar novamente.",
            parse_mode="Markdown")

    ctx.user_data.clear()
    return ConversationHandler.END


# ─── Helper ──────────────────────────────────────────────────

def _salvar_conta(ig: InstagramClient, username: str, password: str):
    if not accounts_db.get_account(username):
        accounts_db.add_account(username, password, ig._fingerprint)
    data = ig.get_session_data()
    if data:
        accounts_db.save_session_backup(username, data)


# ─── Demais comandos ─────────────────────────────────────────

@owner_only
async def cmd_conta_lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_accounts()
    if not accounts:
        await update.message.reply_text("Nenhuma conta cadastrada.")
        return
    status_emoji = {"active": "🟢", "paused": "🟡", "warming": "🔥", "banned": "🔴"}
    lines = ["📱 *Contas cadastradas:*\n"]
    for acc in accounts:
        icon = status_emoji.get(acc["status"], "⚪")
        warmup = f" (dia {acc['warmup_day']}/{len(WARMUP_SCHEDULE)})" if acc.get("warmup_day", 0) > 0 else ""
        lines.append(f"{icon} @{acc['username']} — {acc['status']}{warmup}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_conta_pausar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_pausar @usuario")
        return
    accounts_db.update_status(ctx.args[0].lstrip("@"), "paused")
    await update.message.reply_text(f"⏸ @{ctx.args[0].lstrip('@')} pausada.")


@owner_only
async def cmd_conta_retomar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_retomar @usuario")
        return
    accounts_db.update_status(ctx.args[0].lstrip("@"), "active")
    await update.message.reply_text(f"▶️ @{ctx.args[0].lstrip('@')} retomada.")


@owner_only
async def cmd_conta_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_remover @usuario")
        return
    username = ctx.args[0].lstrip("@")
    accounts_db.remove_account(username)
    PENDING_CHALLENGES.pop(username, None)
    await update.message.reply_text(f"🗑 @{username} removida.")


@owner_only
async def cmd_conta_aquecer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_aquecer @usuario")
        return
    username = ctx.args[0].lstrip("@")
    acc = accounts_db.get_account(username)
    if not acc:
        await update.message.reply_text("Conta não encontrada.")
        return
    accounts_db.sb.table("ig_accounts").update(
        {"warmup_day": 1, "status": "warming"}
    ).eq("username", username).execute()
    schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
    await update.message.reply_text(
        f"🔥 Aquecimento reiniciado para *@{username}*\n"
        f"Progressão: {schedule_str} follows/dia", parse_mode="Markdown")


@owner_only
async def cmd_conta_fingerprint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_fingerprint @usuario")
        return
    username = ctx.args[0].lstrip("@")
    acc = accounts_db.get_account(username)
    if not acc:
        await update.message.reply_text("Conta não encontrada.")
        return
    ig = InstagramClient(username, acc["password"])
    ig.randomize_fingerprint()
    await update.message.reply_text(f"🔀 UUIDs randomizados para @{username}.")


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = ctx.user_data.get("challenge_username", "")
    PENDING_CHALLENGES.pop(username, None)
    ctx.user_data.clear()
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END


# ─── Registro ────────────────────────────────────────────────

def register_contas_handlers(app):
    conv = ConversationHandler(
        entry_points=[CommandHandler("conta_add", cmd_conta_add)],
        states={
            AGUARDANDO_METODO: [
                CallbackQueryHandler(escolher_metodo, pattern=r"^verify:"),
            ],
            AGUARDANDO_CODIGO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_codigo),
            ],
            AGUARDANDO_CONFIRM: [
                CallbackQueryHandler(confirmar_codigo, pattern=r"^code:"),
                CallbackQueryHandler(confirmar_2fa,    pattern=r"^2fa:"),
            ],
            AGUARDANDO_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_2fa),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("conta_lista",       cmd_conta_lista))
    app.add_handler(CommandHandler("conta_pausar",      cmd_conta_pausar))
    app.add_handler(CommandHandler("conta_retomar",     cmd_conta_retomar))
    app.add_handler(CommandHandler("conta_remover",     cmd_conta_remover))
    app.add_handler(CommandHandler("conta_aquecer",     cmd_conta_aquecer))
    app.add_handler(CommandHandler("conta_fingerprint", cmd_conta_fingerprint))
