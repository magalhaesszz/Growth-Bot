import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database.accounts import AccountsDB
from instagram.client import InstagramClient
from config import TELEGRAM_OWNER_ID, WARMUP_SCHEDULE

logger = logging.getLogger(__name__)
accounts_db = AccountsDB()


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            await update.message.reply_text("⛔ Acesso negado.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


@owner_only
async def cmd_conta_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /conta_add @usuario senha"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /conta_add @usuario senha")
        return

    username = ctx.args[0].lstrip("@")
    password = ctx.args[1]

    await update.message.reply_text(f"🔄 Conectando @{username}...")

    ig = InstagramClient(username, password)
    if not ig.login():
        await update.message.reply_text(f"❌ Falha no login de @{username}. Verifique as credenciais.")
        return

    accounts_db.add_account(username, password, ig._fingerprint)
    session_data = ig.get_session_data()
    if session_data:
        accounts_db.save_session_backup(username, session_data)

    schedule_str = " → ".join(str(n) for n in WARMUP_SCHEDULE)
    await update.message.reply_text(
        f"✅ @{username} conectada!\n"
        f"🔥 Aquecimento ativado: {schedule_str} follows/dia\n"
        f"Fingerprint: *{ig._fingerprint.get('device', 'n/a')}*",
        parse_mode="Markdown"
    )


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


def register_contas_handlers(app):
    app.add_handler(CommandHandler("conta_add", cmd_conta_add))
    app.add_handler(CommandHandler("conta_lista", cmd_conta_lista))
    app.add_handler(CommandHandler("conta_pausar", cmd_conta_pausar))
    app.add_handler(CommandHandler("conta_retomar", cmd_conta_retomar))
    app.add_handler(CommandHandler("conta_remover", cmd_conta_remover))
    app.add_handler(CommandHandler("conta_aquecer", cmd_conta_aquecer))
    app.add_handler(CommandHandler("conta_fingerprint", cmd_conta_fingerprint))
