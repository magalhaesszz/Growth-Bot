import json
import logging
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters,
)

from database.accounts import AccountsDB
from instagram.client import InstagramClient, PENDING_CHALLENGES
from config import TELEGRAM_OWNER_ID, WARMUP_SCHEDULE

logger = logging.getLogger(__name__)
accounts_db = AccountsDB()

# Estados da conversa
AGUARDANDO_CODIGO = 1
AGUARDANDO_2FA    = 2


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
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
            "Uso: `/conta_add @usuario senha`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    username = ctx.args[0].lstrip("@")
    password = ctx.args[1]

    ctx.user_data["challenge_username"] = username
    ctx.user_data["challenge_password"] = password

    await update.message.reply_text(f"🔄 Conectando *@{username}*...", parse_mode="Markdown")

    # Login em thread separada para não bloquear o event loop
    import asyncio
    loop = asyncio.get_event_loop()

    ig = InstagramClient(username, password)

    # Executa o login em thread separada (pode bloquear aguardando challenge)
    result = await loop.run_in_executor(None, ig.login)

    if result == "ok":
        _salvar_conta(ig, username, password)
        schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
        await update.message.reply_text(
            f"✅ *@{username}* conectada com sucesso!\n"
            f"🔥 Aquecimento: {schedule_str} follows/dia\n"
            f"📱 Fingerprint: *{ig._fingerprint.get('device', 'n/a')}*",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif result == "challenge":
        # O _telegram_code_handler já está bloqueando em outra thread
        # aguardando o código — precisamos pedí-lo ao usuário
        ctx.user_data["challenge_ig"] = ig
        await update.message.reply_text(
            f"📱 *Verificação necessária — @{username}*\n\n"
            f"O Instagram enviou um código de verificação para o\n"
            f"*e-mail ou SMS* cadastrado na conta.\n\n"
            f"✏️ Digite o código de 6 dígitos abaixo:\n\n"
            f"_Use /cancelar para cancelar._",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CODIGO

    elif result == "two_factor":
        ctx.user_data["challenge_ig"] = ig
        await update.message.reply_text(
            f"🔐 *2FA ativo — @{username}*\n\n"
            f"Digite o código do seu aplicativo autenticador:",
            parse_mode="Markdown"
        )
        return AGUARDANDO_2FA

    elif result == "error:bad_password":
        await update.message.reply_text(
            f"❌ *Senha incorreta* para @{username}.\n"
            f"Verifique e tente novamente com `/conta_add @{username} senha_correta`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif result == "error:rate_limit":
        await update.message.reply_text(
            f"⏳ Instagram pedindo espera para *@{username}*.\n"
            f"Tente novamente em 10-15 minutos.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif result == "error:feedback_required":
        await update.message.reply_text(
            f"⚠️ Instagram bloqueou *@{username}* temporariamente.\n"
            f"Acesse o app pelo celular e confirme sua identidade, depois tente de novo.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            f"❌ Erro ao conectar *@{username}*:\n`{result}`\n\n"
            f"Se o Instagram pediu verificação, acesse o app pelo celular primeiro.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END


# ─── Receber código de verificação ───────────────────────────

async def receber_codigo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    code = update.message.text.strip()
    username = ctx.user_data.get("challenge_username")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")

    if not username or not ig:
        await update.message.reply_text("❌ Sessão expirada. Use /conta_add novamente.")
        return ConversationHandler.END

    if not code.isdigit() or len(code) < 4:
        await update.message.reply_text("❌ Código inválido. Digite apenas os números (ex: 123456).")
        return AGUARDANDO_CODIGO

    await update.message.reply_text(f"🔄 Verificando código para *@{username}*...", parse_mode="Markdown")

    # Injeta o código no handler que está aguardando em outra thread
    sucesso = ig.submit_code(code)

    if sucesso:
        # Aguarda o login completar (o challenge_flow vai terminar em breve)
        import asyncio, time
        await asyncio.sleep(3)

        # Verifica se logou
        if ig.is_logged_in():
            _salvar_conta(ig, username, ctx.user_data.get("challenge_password", ""))
            schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
            await update.message.reply_text(
                f"✅ *@{username}* verificada e conectada!\n"
                f"🔥 Aquecimento: {schedule_str} follows/dia",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Código enviado mas login não confirmado para *@{username}*.\n"
                f"Tente `/conta_add @{username} senha` novamente.",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            f"❌ Código inválido ou expirado para *@{username}*.\n"
            f"Tente `/conta_add @{username} senha` para solicitar um novo código.",
            parse_mode="Markdown"
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ─── Receber código 2FA ───────────────────────────────────────

async def receber_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return ConversationHandler.END

    code = update.message.text.strip()
    username = ctx.user_data.get("challenge_username")
    ig: InstagramClient = ctx.user_data.get("challenge_ig")

    if not username or not ig:
        await update.message.reply_text("❌ Sessão expirada. Use /conta_add novamente.")
        return ConversationHandler.END

    await update.message.reply_text(f"🔄 Verificando 2FA para *@{username}*...", parse_mode="Markdown")

    sucesso = ig.submit_2fa(code)

    if sucesso:
        _salvar_conta(ig, username, ctx.user_data.get("challenge_password", ""))
        await update.message.reply_text(
            f"✅ *@{username}* com 2FA conectada!",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Código 2FA incorreto para *@{username}*.\n"
            f"Tente novamente ou use `/conta_add @{username} senha`.",
            parse_mode="Markdown"
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ─── Helper para salvar conta ────────────────────────────────

def _salvar_conta(ig: InstagramClient, username: str, password: str):
    existing = accounts_db.get_account(username)
    if not existing:
        accounts_db.add_account(username, password, ig._fingerprint)
    session_data = ig.get_session_data()
    if session_data:
        accounts_db.save_session_backup(username, session_data)


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


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = ctx.user_data.get("challenge_username", "")
    PENDING_CHALLENGES.pop(username, None)
    ctx.user_data.clear()
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END


# ─── Registro ────────────────────────────────────────────────


@owner_only
async def cmd_conta_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Testa o login e mostra a resposta bruta do Instagram para diagnóstico."""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /conta_debug @usuario senha")
        return

    username = ctx.args[0].lstrip("@")
    password = ctx.args[1]

    await update.message.reply_text(f"🔍 Testando login de @{username}...")

    import asyncio
    from instagram.client import InstagramClient, PENDING_CHALLENGES

    ig = InstagramClient(username, password)
    try:
        ig.cl.login(username, password)
        await update.message.reply_text("✅ Login direto funcionou! Use /conta_add normalmente.")
    except Exception as e:
        last = ig.cl.last_json or {}
        msg = (
            f"❌ Erro: `{type(e).__name__}: {e}`\n\n"
            f"Resposta do Instagram:\n"
            f"`{json.dumps(last, ensure_ascii=False)[:800]}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

def register_contas_handlers(app):
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("conta_add", cmd_conta_add)],
        states={
            AGUARDANDO_CODIGO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_codigo),
            ],
            AGUARDANDO_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_2fa),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("conta_lista",       cmd_conta_lista))
    app.add_handler(CommandHandler("conta_pausar",      cmd_conta_pausar))
    app.add_handler(CommandHandler("conta_retomar",     cmd_conta_retomar))
    app.add_handler(CommandHandler("conta_remover",     cmd_conta_remover))
    app.add_handler(CommandHandler("conta_aquecer",     cmd_conta_aquecer))
    app.add_handler(CommandHandler("conta_fingerprint", cmd_conta_fingerprint))
    app.add_handler(CommandHandler("conta_debug",       cmd_conta_debug))
