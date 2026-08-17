import asyncio
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database.accounts import AccountsDB
from database.operations import DB
from instagram.risk_detector import risk_detector
from reports.daily import ReportGenerator
from config import TELEGRAM_OWNER_ID

logger = logging.getLogger(__name__)
accounts_db = AccountsDB()
db = DB()
reporter = ReportGenerator()


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from bot.access import has_access
        uid = update.effective_user.id if update.effective_user else 0
        if not has_access(uid):
            await update.message.reply_text("⛔ Acesso negado.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


def _first_account(username_arg=None) -> dict | None:
    if username_arg:
        return accounts_db.get_account(username_arg.lstrip("@"))
    accounts = accounts_db.list_active_accounts()
    return accounts[0] if accounts else None


# ─── Alvos ───────────────────────────────────────────────────

@owner_only
async def cmd_alvo_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /alvo_add https://instagram.com/pagina"""
    if not ctx.args:
        await update.message.reply_text("Uso: /alvo_add https://instagram.com/pagina")
        return
    url = ctx.args[0]
    acc_arg = ctx.args[1] if len(ctx.args) > 1 else None
    acc = _first_account(acc_arg)
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa encontrada.")
        return

    from instagram.client import InstagramClient
    from instagram.scraper import Scraper
    from database.accounts import AccountsDB
    ig = InstagramClient(acc["username"], acc.get("password", ""))
    # Restaurar sessão salva — não fazer login do zero
    adb = AccountsDB()
    session_data = adb.load_session_backup(acc["username"])
    if session_data:
        ig.load_session_from_data(session_data)
    elif not ig.is_logged_in():
        result_login = ig.login()
        if result_login != "ok":
            await update.message.reply_text(
                f"❌ Não foi possível autenticar @{acc['username']}: `{result_login}`",
                parse_mode="Markdown",
            )
            return
    page = Scraper(ig).resolve_page(url)
    if not page:
        await update.message.reply_text("❌ Página não encontrada no Instagram.")
        return

    campaign = db.get_active_campaign(acc["id"])
    db.add_target(acc["id"], url, page["username"], page["user_id"],
                  campaign["id"] if campaign else None)
    await update.message.reply_text(
        f"✅ Alvo adicionado: *@{page['username']}*\nConta: @{acc['username']}",
        parse_mode="Markdown"
    )


@owner_only
async def cmd_alvo_lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    targets = db.list_targets(acc["id"])
    if not targets:
        await update.message.reply_text("Nenhum alvo cadastrado.")
        return
    lines = [f"🎯 *Alvos de @{acc['username']}:*\n"]
    for t in targets:
        lines.append(f"• @{t['page_username']} — raspados: {t['scraped_count']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_alvo_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /alvo_remover @pagina")
        return
    acc = _first_account()
    if not acc:
        return
    db.remove_target(acc["id"], ctx.args[0].lstrip("@"))
    await update.message.reply_text(f"🗑 Alvo removido.")


# ─── Campanhas ───────────────────────────────────────────────

@owner_only
async def cmd_campanha_nova(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /campanha_nova nome-da-campanha")
        return
    name = " ".join(ctx.args)
    acc = _first_account()
    if not acc:
        return
    db.create_campaign(acc["id"], name)
    await update.message.reply_text(f"🚀 Campanha *{name}* criada!", parse_mode="Markdown")


@owner_only
async def cmd_campanha_hist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        return
    camps = db.list_campaigns(acc["id"])
    if not camps:
        await update.message.reply_text("Nenhuma campanha registrada.")
        return
    lines = [f"📂 *Histórico — @{acc['username']}:*\n"]
    for c in camps[:5]:
        total = max(c["total_follows"], 1)
        rate = f"{c['total_follow_backs'] / total * 100:.1f}%"
        lines.append(
            f"• *{c['name']}*\n"
            f"  {c['total_follows']} follows · {c['total_follow_backs']} de volta ({rate})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Nichos, score, filtros ───────────────────────────────────

@owner_only
async def cmd_nicho_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nicho = " ".join(ctx.args) if ctx.args else ""
    if not nicho:
        await update.message.reply_text("Uso: /nicho_set moda feminina")
        return
    acc = _first_account()
    if acc:
        camp = db.get_active_campaign(acc["id"])
        if camp:
            db.sb.table("ig_campaigns").update({"nicho": nicho}).eq("id", camp["id"]).execute()
    await update.message.reply_text(f"✅ Nicho: *{nicho}*", parse_mode="Markdown")


@owner_only
async def cmd_score_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /score_set 60"""
    if not ctx.args:
        await update.message.reply_text("Uso: /score_set 60")
        return
    try:
        score = int(ctx.args[-1])
        if not 0 <= score <= 100:
            raise ValueError
        acc = _first_account(ctx.args[0] if len(ctx.args) > 1 else None)
        if acc:
            accounts_db.update_settings(acc["username"], {"score_min": score})
        await update.message.reply_text(f"✅ Score mínimo: *{score}*", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("Score deve ser um número de 0 a 100.")


# ─── Whitelist / Blacklist ────────────────────────────────────

@owner_only
async def cmd_white_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /white_add @usuario")
        return
    acc = _first_account()
    if acc:
        db.add_whitelist(acc["id"], ctx.args[0])
    await update.message.reply_text(f"🛡 @{ctx.args[0].lstrip('@')} na whitelist.")


@owner_only
async def cmd_black_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /black_add palavra_ou_@usuario")
        return
    acc = _first_account()
    if acc:
        db.add_blacklist(acc["id"], ctx.args[0])
    await update.message.reply_text(f"🚫 '{ctx.args[0]}' na blacklist.")


@owner_only
async def cmd_listas_ver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        return
    wl = db.get_whitelist(acc["id"])
    bl = db.get_blacklist(acc["id"])
    msg = (
        f"🛡 *Whitelist ({len(wl)}):* {', '.join(wl) if wl else 'vazia'}\n"
        f"🚫 *Blacklist ({len(bl)}):* {', '.join(bl) if bl else 'vazia'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── Limites e comportamento ─────────────────────────────────

@owner_only
async def cmd_limite_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /limite_set follows=50  ou  /limite_set unfollows=40"""
    settings = {}
    key_map = {"follows": "daily_follows", "unfollows": "daily_unfollows"}
    for arg in ctx.args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            if k in key_map:
                settings[key_map[k]] = int(v)
    if not settings:
        await update.message.reply_text("Uso: /limite_set follows=50 unfollows=40")
        return
    acc = _first_account()
    if acc:
        accounts_db.update_settings(acc["username"], settings)
    await update.message.reply_text(f"✅ Limites atualizados: {settings}")


@owner_only
async def cmd_horario_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /horario_set 8-22"""
    if not ctx.args or "-" not in ctx.args[0]:
        await update.message.reply_text("Uso: /horario_set 8-22")
        return
    parts = ctx.args[0].split("-")
    acc = _first_account()
    if acc:
        accounts_db.update_settings(acc["username"], {
            "hour_start": int(parts[0].replace("h", "")),
            "hour_end": int(parts[1].replace("h", "")),
        })
    await update.message.reply_text(f"✅ Janela: {ctx.args[0]}")


@owner_only
async def cmd_delay_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /delay_set 30-90"""
    if not ctx.args or "-" not in ctx.args[0]:
        await update.message.reply_text("Uso: /delay_set 30-90")
        return
    parts = ctx.args[0].split("-")
    acc = _first_account()
    if acc:
        accounts_db.update_settings(acc["username"], {
            "delay_min": int(parts[0]),
            "delay_max": int(parts[1]),
        })
    await update.message.reply_text(f"✅ Delay: {ctx.args[0]}s")


@owner_only
async def cmd_unfollow_prazo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /unfollow_prazo 5"""
    if not ctx.args:
        await update.message.reply_text("Uso: /unfollow_prazo 5")
        return
    acc = _first_account()
    if acc:
        accounts_db.update_settings(acc["username"], {"unfollow_after_days": int(ctx.args[0])})
    await update.message.reply_text(f"✅ Prazo de unfollow: {ctx.args[0]} dias")


# ─── Fila ────────────────────────────────────────────────────

@owner_only
async def cmd_fila_ver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from action_queue.action_queue import ActionQueue
    acc = _first_account()
    if not acc:
        return
    items = ActionQueue().list_pending(acc["id"])
    if not items:
        await update.message.reply_text("Fila vazia.")
        return
    lines = [f"📋 *Fila — @{acc['username']}:*\n"]
    for item in items[:10]:
        lines.append(f"• {item['action']} | tentativas: {item['retries']} | {item['status']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_fila_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from action_queue.action_queue import ActionQueue
    acc = _first_account()
    if not acc:
        return
    ActionQueue().clear_account(acc["id"])
    await update.message.reply_text("🗑 Fila limpa.")


# ─── Segurança ───────────────────────────────────────────────

@owner_only
async def cmd_risco_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    statuses = risk_detector.get_all_statuses()
    if not statuses:
        await update.message.reply_text("Nenhuma conta monitorada ainda.\nUse /conta_add para começar.")
        return
    lines = ["🔒 *Status de risco:*\n"]
    for s in statuses:
        icon = "🔴" if s["is_paused"] else "🟢"
        lines.append(
            f"{icon} @{s['username']}\n"
            f"  Taxa de erro: {s['error_rate']} | Erros consec.: {s['consecutive_errors']}"
        )
        if s["pause_reason"]:
            lines.append(f"  ↳ {s['pause_reason']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_alerta_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args_str = " ".join(ctx.args) if ctx.args else "padrão"
    await update.message.reply_text(
        f"✅ Configuração registrada: `{args_str}`\n"
        f"O detector monitora continuamente e te avisa automaticamente.",
        parse_mode="Markdown"
    )


@owner_only
async def cmd_sessao_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_active_accounts()
    count = 0
    for acc in accounts:
        from instagram.client import InstagramClient
        ig = InstagramClient(acc["username"], acc["password"])
        data = ig.get_session_data()
        if data:
            accounts_db.save_session_backup(acc["username"], data)
            count += 1
    await update.message.reply_text(f"✅ Backup de {count} sessão(ões) concluído.")


@owner_only
async def cmd_sessao_restaurar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /sessao_restaurar @usuario")
        return
    username = ctx.args[0].lstrip("@")
    acc = accounts_db.get_account(username)
    if not acc:
        await update.message.reply_text("Conta não encontrada.")
        return
    data = accounts_db.load_session_backup(username)
    if not data:
        await update.message.reply_text("Nenhum backup encontrado para essa conta.")
        return
    from instagram.client import InstagramClient
    ig = InstagramClient(username, acc["password"])
    ig.load_session_from_data(data)
    await update.message.reply_text(
        f"✅ Sessão de *@{username}* restaurada do banco.",
        parse_mode="Markdown"
    )


# ─── Monitoramento e relatórios ──────────────────────────────

@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_active_accounts()
    if not accounts:
        await update.message.reply_text("Nenhuma conta ativa. Use /conta_add.")
        return
    lines = ["📊 *Status do bot:*\n"]
    for acc in accounts:
        stats = db.get_stats_today(acc["id"])
        risk = risk_detector.get_status(acc["username"])
        icon = "🔴" if risk["is_paused"] else "🟢"
        lines.append(
            f"{icon} *@{acc['username']}* ({acc['status']})\n"
            f"  ✅ Follows hoje: {stats.get('follow', 0)}\n"
            f"  🔄 Unfollows hoje: {stats.get('unfollow', 0)}\n"
            f"  👁 Stories: {stats.get('story_view', 0)}\n"
            f"  ⚠️ Erros: {stats.get('error', 0)}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    text = reporter.generate_text(acc["id"], acc["username"])
    chart_buf = reporter.generate_chart(acc["id"], acc["username"])
    await update.message.reply_photo(photo=chart_buf, caption=text, parse_mode="Markdown")


@owner_only
async def cmd_pendentes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        return
    candidates = db.get_unfollow_candidates(acc["id"], 1)
    if not candidates:
        await update.message.reply_text("Nenhum perfil pendente de análise.")
        return
    lines = [f"⏳ *Pendentes — @{acc['username']}:*\n"]
    for c in candidates[:15]:
        try:
            dt = datetime.fromisoformat(c["followed_at"].replace("Z", "+00:00"))
            days = (datetime.now(dt.tzinfo) - dt).days
        except Exception:
            days = "?"
        lines.append(f"• @{c['target_username']} — {days} dia(s)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        return
    logs = db.get_recent_logs(acc["id"], limit=10)
    if not logs:
        await update.message.reply_text("Nenhuma ação registrada ainda.")
        return
    lines = [f"📋 *Últimas ações — @{acc['username']}:*\n"]
    for l in logs:
        icon = "✅" if l["success"] else "❌"
        target = f"@{l['target_username']}" if l.get("target_username") else ""
        lines.append(f"{icon} {l['action']} {target} — {l['executed_at'][:16]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_modo_teste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    targets = db.list_targets(acc["id"])
    wl = db.get_whitelist(acc["id"])
    bl = db.get_blacklist(acc["id"])
    camp = db.get_active_campaign(acc["id"])
    msg = (
        f"🧪 *Simulação — sem executar nada*\n\n"
        f"Conta: *@{acc['username']}* ({acc['status']})\n"
        f"Alvos: {len(targets)}\n"
        f"Whitelist: {len(wl)} | Blacklist: {len(bl)}\n"
        f"Score mín: {acc.get('score_min', 50)}\n"
        f"Follows/dia: {acc.get('daily_follows', 40)}\n"
        f"Unfollows/dia: {acc.get('daily_unfollows', 40)}\n"
        f"Janela: {acc.get('hour_start', 8)}h–{acc.get('hour_end', 22)}h\n"
        f"Delay: {acc.get('delay_min', 30)}–{acc.get('delay_max', 90)}s\n"
        f"Unfollow após: {acc.get('unfollow_after_days', 5)} dias\n"
        f"Campanha ativa: *{camp['name'] if camp else 'nenhuma'}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


@owner_only
async def cmd_config_ver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_accounts()
    if not accounts:
        await update.message.reply_text("Nenhuma conta cadastrada.")
        return
    lines = ["⚙️ *Configurações:*\n"]
    for acc in accounts:
        lines.append(
            f"*@{acc['username']}* ({acc['status']})\n"
            f"  Follows: {acc.get('daily_follows',40)}/dia · Unfollows: {acc.get('daily_unfollows',40)}/dia\n"
            f"  Score mín: {acc.get('score_min',50)} · Prazo: {acc.get('unfollow_after_days',5)}d\n"
            f"  Janela: {acc.get('hour_start',8)}h–{acc.get('hour_end',22)}h\n"
            f"  Delay: {acc.get('delay_min',30)}–{acc.get('delay_max',90)}s · Aquecimento dia: {acc.get('warmup_day',0)}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Controle geral ──────────────────────────────────────────

@owner_only
async def cmd_pausar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_active_accounts()
    for acc in accounts:
        accounts_db.update_status(acc["username"], "paused")
    await update.message.reply_text(f"⏸ {len(accounts)} conta(s) pausada(s).")


@owner_only
async def cmd_retomar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = accounts_db.list_accounts()
    count = 0
    for acc in accounts:
        if acc["status"] == "paused":
            accounts_db.update_status(acc["username"], "active")
            risk_detector.resume(acc["username"])
            count += 1
    await update.message.reply_text(f"▶️ {count} conta(s) retomada(s).")



@owner_only
async def cmd_seguidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    db   = DB()
    rows = db.get_following_list(acc["id"], limit=30)
    if not rows:
        await update.message.reply_text("Nenhum seguido registrado ainda.")
        return
    n = len(rows)
    lines = [f"*Ultimos {n} seguidos:*\n"]
    for r in rows:
        fb   = "\u2705" if r.get("follows_back") else "\u274c"
        date = (r.get("followed_at") or "")[:10]
        lines.append(f"{fb} @{r['target_username']} \u2014 {date}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_nao_seguem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    limite = 0
    if ctx.args:
        try: limite = int(ctx.args[0])
        except Exception: pass
    db   = DB()
    rows = db.get_non_followers(acc["id"], limit=limite)
    if not rows:
        await update.message.reply_text("\u2705 Todos seguem de volta!")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    n = len(rows)
    lines = [f"*{n} nao seguem de volta:*\n"]
    for r in rows[:20]:
        date = (r.get("followed_at") or "")[:10]
        lines.append(f"\u2022 @{r['target_username']} \u2014 {date}")
    if n > 20:
        lines.append(f"_... e mais {n-20}_")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"\U0001f5d1 Deixar de seguir todos ({n})",
            callback_data=f"unfollow_batch:{acc['id']}:{limite}"
        )],
        [InlineKeyboardButton("\u274c Cancelar", callback_data="unfollow_cancel")],
    ])
    await update.message.reply_text(
        "\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")


@owner_only
async def cmd_deixar_seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /deixar_seguir @usuario")
        return
    acc = _first_account()
    if not acc:
        await update.message.reply_text("Nenhuma conta ativa.")
        return
    username = ctx.args[0].lstrip("@")
    db   = DB()
    rows = db.get_following_list(acc["id"], limit=500)
    target = next((r for r in rows if r["target_username"] == username), None)
    if not target:
        await update.message.reply_text(f"@{username} nao encontrado nos seguidos.")
        return
    from database.accounts import AccountsDB
    from instagram.client import InstagramClient
    adb  = AccountsDB()
    ig   = InstagramClient(acc["username"], acc.get("password",""))
    sess = adb.load_session_backup(acc["username"])
    if sess:
        ig.load_session_from_data(sess)
    await update.message.reply_text(f"\u23f3 Parando de seguir @{username}...")
    try:
        def _do():
            ig.api.user_unfollow(int(target["target_user_id"]))
            db.unfollow_user_by_username(acc["id"], username)
            db.log_action(acc["id"], "unfollow", username, "manual", True)
        await asyncio.to_thread(_do)
        await update.message.reply_text(f"\u2705 Parou de seguir @{username}.")
    except Exception as e:
        await update.message.reply_text(f"\u274c Erro: {e}")


async def on_unfollow_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "unfollow_cancel":
        await query.edit_message_text("\u274c Cancelado.")
        return
    if not query.data.startswith("unfollow_batch:"):
        return
    parts  = query.data.split(":")
    acc_id = parts[1]
    limite = int(parts[2]) if len(parts) > 2 and parts[2] else 0
    await query.edit_message_text("\u23f3 Executando unfollows...")
    from database.accounts import AccountsDB
    from instagram.client import InstagramClient
    from instagram.unfollower import Unfollower
    from instagram.score import WhitelistFilter
    from scheduler.jobs import risk_detector
    adb = AccountsDB()
    db  = DB()
    acc = adb.get_account_by_id(acc_id)
    if not acc:
        await query.edit_message_text("\u274c Conta nao encontrada.")
        return
    ig   = InstagramClient(acc["username"], acc.get("password",""))
    sess = adb.load_session_backup(acc["username"])
    if sess:
        ig.load_session_from_data(sess)
    rows = db.get_non_followers(acc_id, limit=limite)
    wl   = WhitelistFilter(db.get_whitelist(acc_id))
    unfollower = Unfollower(ig, risk_detector, wl)
    def _do():
        return unfollower.unfollow_batch(
            rows,
            daily_limit=acc.get("daily_unfollows", 50),
            delay_min=acc.get("delay_min", 30),
            delay_max=acc.get("delay_max", 90),
            on_success=lambda u, uid, kept: db.unfollow_user_by_username(acc_id, u) if not kept else None,
            policy="keep_follow_backs",
        )
    result = await asyncio.to_thread(_do)
    await query.message.reply_text(
        f"\u2705 Unfollow concluido!\n"
        f"Removidos: *{result['unfollowed']}* | Erros: {result['errors']}",
        parse_mode="Markdown")

def register_operacoes_handlers(app):
    handlers = [
        ("alvo_add",         cmd_alvo_add),
        ("alvo_lista",       cmd_alvo_lista),
        ("alvo_remover",     cmd_alvo_remover),
        ("campanha_nova",    cmd_campanha_nova),
        ("campanha_hist",    cmd_campanha_hist),
        ("nicho_set",        cmd_nicho_set),
        ("score_set",        cmd_score_set),
        ("white_add",        cmd_white_add),
        ("black_add",        cmd_black_add),
        ("listas_ver",       cmd_listas_ver),
        ("limite_set",       cmd_limite_set),
        ("horario_set",      cmd_horario_set),
        ("delay_set",        cmd_delay_set),
        ("unfollow_prazo",   cmd_unfollow_prazo),
        ("fila_ver",         cmd_fila_ver),
        ("fila_limpar",      cmd_fila_limpar),
        ("risco_status",     cmd_risco_status),
        ("alerta_set",       cmd_alerta_set),
        ("sessao_backup",    cmd_sessao_backup),
        ("sessao_restaurar", cmd_sessao_restaurar),
        ("status",           cmd_status),
        ("relatorio",        cmd_relatorio),
        ("pendentes",        cmd_pendentes),
        ("log",              cmd_log),
        ("modo_teste",       cmd_modo_teste),
        ("config_ver",       cmd_config_ver),
        ("pausar",           cmd_pausar),
        ("retomar",          cmd_retomar),
    ]
    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))
