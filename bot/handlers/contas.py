import logging
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters
)

from database.accounts import AccountsDB
from instagram.client import InstagramClient, PENDING_CHALLENGES
from config import TELEGRAM_OWNER_ID, WARMUP_SCHEDULE

logger = logging.getLogger(__name__)
accounts_db = AccountsDB()

# Estados da conversa de verificação
AGUARDANDO_CODIGO = 1


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            await update.message.reply_text("⛔ Acesso negado.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ─── /conta_add com suporte a challenge ──────────────────────

async def cmd_conta_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("⛔ Acesso negado.")
        return ConversationHandler.END

    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /conta_add @usuario senha")
        return ConversationHandler.END

    username = ctx.args[0].lstrip("@")
    password = ctx.args[1]

    await update.message.reply_text(f"🔄 Conectando @{username}...")

    ig = InstagramClient(username, password)
    result = ig.login()

    if result == "ok":
        # Salva conta e sessão
        existing = accounts_db.get_account(username)
        if not existing:
            accounts_db.add_account(username, password, ig._fingerprint)
        session_data = ig.get_session_data()
        if session_data:
            accounts_db.save_session_backup(username, session_data)

        schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
        await update.message.reply_text(
            f"✅ *@{username}* conectada!\n"
            f"🔥 Aquecimento: {schedule_str} follows/dia\n"
            f"Fingerprint: *{ig._fingerprint.get('device', 'n/a')}*",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif result == "challenge":
        # Guarda username no contexto para usar na próxima mensagem
        ctx.user_data["challenge_username"] = username
        ctx.user_data["challenge_password"] = password

        await update.message.reply_text(
            f"📱 *Verificação necessária para @{username}*\n\n"
            f"O Instagram enviou um código de confirmação para o *e-mail ou SMS* cadastrado na conta.\n\n"
            f"Digite o código abaixo:",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CODIGO

    elif result == "error:bad_password":
        await update.message.reply_text(
            f"❌ *Senha incorreta* para @{username}.\n"
            f"Verifique e tente novamente.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif result == "error:rate_limit":
        await update.message.reply_text(
            f"⏳ O Instagram está pedindo espera para @{username}.\n"
            f"Tente novamente em 10-15 minutos.",
        )
        return ConversationHandler.END

    elif result == "error:invalid_user":
        await update.message.reply_text(f"❌ Usuário @{username} não encontrado.")
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            f"❌ Erro ao conectar @{username}:\n`{result}`\n\n"
            f"Se o Instagram pediu verificação, acesse o app pelo celular primeiro.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END


async def receber_codigo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recebe o código de verificação digitado pelo usuário."""
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    code = update.message.text.strip()
    username = ctx.user_data.get("challenge_username")
    password = ctx.user_data.get("challenge_password")

    if not username:
        await update.message.reply_text("❌ Sessão expirada. Use /conta_add novamente.")
        return ConversationHandler.END

    await update.message.reply_text(f"🔄 Verificando código para @{username}...")

    # Recupera o cliente que está com challenge pendente
    ig = PENDING_CHALLENGES.get(username)
    if not ig:
        # Reconstrói se não estiver em memória
        ig = InstagramClient(username, password)
        ig.login()

    success = ig.submit_challenge_code(code)

    if success:
        existing = accounts_db.get_account(username)
        if not existing:
            accounts_db.add_account(username, password, ig._fingerprint)
        session_data = ig.get_session_data()
        if session_data:
            accounts_db.save_session_backup(username, session_data)

        schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
        await update.message.reply_text(
            f"✅ *@{username}* verificada e conectada!\n"
            f"🔥 Aquecimento: {schedule_str} follows/dia",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Código inválido ou expirado.\n"
            f"Use /conta_add @{username} senha para tentar novamente."
        )

    ctx.user_data.pop("challenge_username", None)
    ctx.user_data.pop("challenge_password", None)
    return ConversationHandler.END


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Conexão cancelada.")
    ctx.user_data.clear()
    return ConversationHandler.END


# ─── Demais comandos de conta ─────────────────────────────────

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
    username = ctx.args[0].lstrip("@")
    accounts_db.update_status(username, "paused")
    await update.message.reply_text(f"⏸ @{username} pausada.")


@owner_only
async def cmd_conta_retomar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /conta_retomar @usuario")
        return
    username = ctx.args[0].lstrip("@")
    accounts_db.update_status(username, "active")
    await update.message.reply_text(f"▶️ @{username} retomada.")


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
        f"Progressão: {schedule_str} follows/dia",
        parse_mode="Markdown"
    )


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
    new_fp = ig.randomize_fingerprint()
    accounts_db.sb.table("ig_accounts").update(
        {"fingerprint": new_fp}
    ).eq("username", username).execute()
    await update.message.reply_text(
        f"🔀 Novo fingerprint de @{username}: *{new_fp['device']}*",
        parse_mode="Markdown"
    )


# ─── Registro ────────────────────────────────────────────────

def register_contas_handlers(app):
    # ConversationHandler para /conta_add com fluxo de verificação
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("conta_add", cmd_conta_add)],
        states={
            AGUARDANDO_CODIGO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_codigo),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("conta_lista",       cmd_conta_lista))
    app.add_handler(CommandHandler("conta_pausar",      cmd_conta_pausar))
    app.add_handler(CommandHandler("conta_retomar",     cmd_conta_retomar))
    app.add_handler(CommandHandler("conta_remover",     cmd_conta_remover))
    app.add_handler(CommandHandler("conta_aquecer",     cmd_conta_aquecer))
    app.add_handler(CommandHandler("conta_fingerprint", cmd_conta_fingerprint))
