import logging
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import TELEGRAM_OWNER_ID, WARMUP_SCHEDULE
from database.accounts import AccountsDB
from database.operations import DB
from instagram.risk_detector import RiskDetector
risk_detector = RiskDetector()
from action_queue.action_queue import ActionQueue
import video_client as vc

logger = logging.getLogger(__name__)

accounts_db = AccountsDB()
db = DB()

UNFOLLOW_POLICY_LABELS = {
    "remove_all": "Remover todos",
    "keep_follow_backs": "Manter follow backs",
    "remove_only_follow_backs": "Remover so follow backs",
}


def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != TELEGRAM_OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("Acesso negado.", show_alert=True)
            elif update.message:
                await update.message.reply_text("Acesso negado.")
            return
        return await func(update, ctx)

    return wrapper


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Status", "dash:status"), _button("Contas", "dash:accounts")],
        [_button("Alvos", "dash:targets"), _button("Campanhas", "dash:campaigns")],
        [_button("Config", "dash:config"), _button("Listas", "dash:lists")],
        [_button("Fila", "dash:queue"), _button("Logs", "dash:logs")],
        [_button("Relatorio", "dash:report"), _button("Seguranca", "dash:safety")],
        [_button("Pausar tudo", "dash:pause_all"), _button("Retomar tudo", "dash:resume_all")],
        [_button("Editor de Video", "dash:video")],
        [_button("Atualizar", "dash:home")],
    ])


def _back_keyboard(refresh: str = "dash:refresh") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Voltar ao painel", "dash:home"), _button("Atualizar", refresh)],
    ])


def _account_keyboard(accounts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts[:12]:
        rows.append([_button(f"@{acc['username']} ({acc['status']})", f"dash:select:{acc['username']}")])
    rows.append([_button("Voltar", "dash:home")])
    return InlineKeyboardMarkup(rows)


def _account_actions_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Pausar", f"dash:pause:{username}"), _button("Retomar", f"dash:resume:{username}")],
        [_button("Reiniciar aquecimento", f"dash:warmup:{username}")],
        [_button("Configurar conta", "dash:config"), _button("Voltar", "dash:accounts")],
    ])


def _config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Limites", "dash:cfg:limits"), _button("Horario", "dash:cfg:hours")],
        [_button("Delay", "dash:cfg:delay"), _button("Score", "dash:cfg:score")],
        [_button("Prazo unfollow", "dash:cfg:unfollow_days")],
        [_button("Regra unfollow", "dash:cfg:unfollow_policy")],
        [_button("Relatorio diario on/off", "dash:cfg:report_toggle")],
        [_button("Voltar", "dash:home")],
    ])


def _policy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Remover todos", "dash:set_policy:remove_all")],
        [_button("Manter follow backs", "dash:set_policy:keep_follow_backs")],
        [_button("Remover so follow backs", "dash:set_policy:remove_only_follow_backs")],
        [_button("Voltar", "dash:config")],
    ])


def _targets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Adicionar alvo", "dash:input:add_target")],
        [_button("Voltar", "dash:home"), _button("Atualizar", "dash:targets")],
    ])


def _campaigns_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Nova campanha", "dash:input:new_campaign")],
        [_button("Voltar", "dash:home"), _button("Atualizar", "dash:campaigns")],
    ])


def _lists_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Add whitelist", "dash:input:add_white"), _button("Add blacklist", "dash:input:add_black")],
        [_button("Voltar", "dash:home"), _button("Atualizar", "dash:lists")],
    ])


def _queue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Limpar fila", "dash:queue_clear")],
        [_button("Voltar", "dash:home"), _button("Atualizar", "dash:queue")],
    ])



def _video_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Status da API", "dash:video:status")],
        [_button("Enviar fundo", "dash:video:set_fundo"), _button("Ver fundo", "dash:video:get_fundo")],
        [_button("Processar video", "dash:video:process"), _button("Modo lote", "dash:video:lote")],
        [_button("Configuracoes", "dash:video:config"), _button("Reset config", "dash:video:config_reset")],
        [_button("Limpar temp", "dash:video:limpar")],
        [_button("Voltar", "dash:home"), _button("Atualizar", "dash:video")],
    ])

def _video_config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Largura do video", "dash:video:cfg:video_width")],
        [_button("Posicao vertical", "dash:video:cfg:position_y")],
        [_button("Qualidade (CRF)", "dash:video:cfg:output_crf")],
        [_button("Anti-ban on/off", "dash:video:cfg:antiban")],
        [_button("Fix mirror on/off", "dash:video:cfg:fix_mirror")],
        [_button("Voltar ao editor", "dash:video")],
    ])


def _safe(value, default="-"):
    return value if value not in (None, "") else default


def _all_accounts() -> list[dict]:
    return accounts_db.list_accounts()


def _selected_account(ctx: ContextTypes.DEFAULT_TYPE) -> dict | None:
    username = ctx.user_data.get("selected_account")
    if username:
        acc = accounts_db.get_account(username)
        if acc:
            return acc
    accounts = accounts_db.list_active_accounts()
    if not accounts:
        accounts = accounts_db.list_accounts()
    if accounts:
        ctx.user_data["selected_account"] = accounts[0]["username"]
        return accounts[0]
    return None


def _home_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    accounts = _all_accounts()
    selected = ctx.user_data.get("selected_account")
    active = [a for a in accounts if a["status"] in {"active", "warming"}]
    paused = [a for a in accounts if a["status"] == "paused"]

    follows = unfollows = follow_backs = errors = 0
    for acc in accounts:
        stats = db.get_stats_today(acc["id"])
        follows += stats.get("follow", 0)
        unfollows += stats.get("unfollow", 0)
        follow_backs += stats.get("follow_back_detected", 0)
        errors += stats.get("error", 0)

    return (
        "*Growth Bot - Painel de Controle*\n\n"
        f"Conta selecionada: *{('@' + selected) if selected else 'nenhuma'}*\n"
        f"Contas: *{len(accounts)}* | Ativas: *{len(active)}* | Pausadas: *{len(paused)}*\n"
        f"Hoje: *{follows}* follows | *{unfollows}* unfollows | *{follow_backs}* follow backs | *{errors}* erros\n\n"
        "Escolha uma area abaixo."
    )


def _status_text() -> str:
    accounts = _all_accounts()
    if not accounts:
        return "*Status*\n\nNenhuma conta cadastrada. Use `/conta_add @usuario senha`."

    lines = ["*Status das contas*\n"]
    for acc in accounts:
        stats = db.get_stats_today(acc["id"])
        risk = risk_detector.get_status(acc["username"])
        is_paused = risk["is_paused"] or acc["status"] == "paused"
        icon = "VERMELHO" if is_paused else "VERDE"
        reason = risk.get("pause_reason") or acc.get("risk_pause_reason")
        lines.append(
            f"*{icon} @{acc['username']}* `{acc['status']}`\n"
            f"Follows: *{stats.get('follow', 0)}* | Unfollows: *{stats.get('unfollow', 0)}* | "
            f"Follow backs: *{stats.get('follow_back_detected', 0)}* | Erros: *{stats.get('error', 0)}*\n"
            f"Risco: *{risk.get('error_rate', 'N/A')}* | Acoes na janela: *{risk.get('actions_in_window', 0)}*"
        )
        if reason:
            lines.append(f"Motivo: `{reason}`")
        lines.append("")
    return "\n".join(lines)


def _accounts_text() -> str:
    accounts = _all_accounts()
    if not accounts:
        return "*Contas*\n\nNenhuma conta cadastrada. Use `/conta_add @usuario senha`."

    lines = ["*Selecione uma conta*\n"]
    for acc in accounts:
        warmup = f" | aquecimento {acc.get('warmup_day')}/{len(WARMUP_SCHEDULE)}" if acc.get("warmup_day", 0) else ""
        lines.append(
            f"• *@{acc['username']}* - `{acc['status']}`{warmup}\n"
            f"  {acc.get('daily_follows', 40)} follows/dia | {acc.get('daily_unfollows', 40)} unfollows/dia"
        )
    return "\n".join(lines)


def _account_text(username: str) -> str:
    acc = accounts_db.get_account(username)
    if not acc:
        return "Conta nao encontrada."

    stats = db.get_stats_today(acc["id"])
    targets = db.list_targets(acc["id"])
    campaign = db.get_active_campaign(acc["id"])
    risk = risk_detector.get_status(username)
    policy = UNFOLLOW_POLICY_LABELS.get(acc.get("unfollow_policy", "remove_all"), "Remover todos")
    return (
        f"*Conta @{username}*\n\n"
        f"Status: `{acc['status']}`\n"
        f"Aquecimento: *{acc.get('warmup_day', 0)}*\n"
        f"Follows hoje: *{stats.get('follow', 0)}* / {acc.get('daily_follows', 40)}\n"
        f"Unfollows hoje: *{stats.get('unfollow', 0)}* / {acc.get('daily_unfollows', 40)}\n"
        f"Follow backs hoje: *{stats.get('follow_back_detected', 0)}*\n"
        f"Janela: *{acc.get('hour_start', 8)}h-{acc.get('hour_end', 22)}h*\n"
        f"Delay: *{acc.get('delay_min', 30)}-{acc.get('delay_max', 90)}s*\n"
        f"Score minimo: *{acc.get('score_min', 50)}*\n"
        f"Unfollow apos: *{acc.get('unfollow_after_days', 5)} dias*\n"
        f"Regra unfollow: *{policy}*\n"
        f"Relatorio diario: *{'ativo' if acc.get('daily_report_enabled', True) else 'desativado'}*\n"
        f"Alvos ativos: *{len(targets)}*\n"
        f"Campanha: *{campaign['name'] if campaign else 'nenhuma'}*\n"
        f"Risco: *{risk.get('error_rate', 'N/A')}*"
    )


def _targets_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Alvos*\n\nNenhuma conta cadastrada."
    targets = db.list_targets(acc["id"])
    lines = [f"*Alvos de @{acc['username']}*\n"]
    if not targets:
        lines.append("Nenhum alvo cadastrado.")
    for target in targets[:15]:
        label = _safe(target.get("page_username"), target.get("page_url"))
        lines.append(f"• @{label} - prioridade {target.get('priority', 1)} - {target.get('scraped_count', 0)} raspados")
    return "\n".join(lines)


def _campaigns_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Campanhas*\n\nNenhuma conta cadastrada."
    campaigns = db.list_campaigns(acc["id"])
    active = db.get_active_campaign(acc["id"])
    lines = [f"*Campanhas de @{acc['username']}*\n"]
    if active:
        lines.append(f"Ativa: *{active['name']}*")
    if not campaigns:
        lines.append("Nenhuma campanha registrada.")
    for camp in campaigns[:8]:
        total = max(camp.get("total_follows", 0), 1)
        rate = camp.get("total_follow_backs", 0) / total * 100
        lines.append(
            f"• *{camp['name']}* `{camp['status']}`\n"
            f"  {camp.get('total_follows', 0)} follows | {camp.get('total_follow_backs', 0)} de volta ({rate:.1f}%)"
        )
    return "\n".join(lines)


def _config_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Config*\n\nNenhuma conta cadastrada."
    policy = UNFOLLOW_POLICY_LABELS.get(acc.get("unfollow_policy", "remove_all"), "Remover todos")
    return (
        f"*Configuracoes de @{acc['username']}*\n\n"
        f"Follows/dia: *{acc.get('daily_follows', 40)}*\n"
        f"Unfollows/dia: *{acc.get('daily_unfollows', 40)}*\n"
        f"Janela: *{acc.get('hour_start', 8)}h-{acc.get('hour_end', 22)}h*\n"
        f"Delay: *{acc.get('delay_min', 30)}-{acc.get('delay_max', 90)}s*\n"
        f"Score minimo: *{acc.get('score_min', 50)}*\n"
        f"Unfollow apos: *{acc.get('unfollow_after_days', 5)} dias*\n"
        f"Regra unfollow: *{policy}*\n"
        f"Relatorio diario: *{'ativo' if acc.get('daily_report_enabled', True) else 'desativado'}*"
    )


def _lists_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Listas*\n\nNenhuma conta cadastrada."
    wl = db.get_whitelist(acc["id"])
    bl = db.get_blacklist(acc["id"])
    return (
        f"*Listas de @{acc['username']}*\n\n"
        f"Whitelist ({len(wl)}): {', '.join(wl[:20]) if wl else 'vazia'}\n"
        f"Blacklist ({len(bl)}): {', '.join(bl[:20]) if bl else 'vazia'}"
    )


def _queue_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Fila*\n\nNenhuma conta cadastrada."
    items = ActionQueue().list_pending(acc["id"])
    lines = [f"*Fila de @{acc['username']}*\n"]
    if not items:
        lines.append("Fila vazia.")
    for item in items[:12]:
        lines.append(f"• `{item['action']}` | {item['status']} | tentativas: {item['retries']}")
    return "\n".join(lines)


def _logs_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Logs*\n\nNenhuma conta cadastrada."
    logs = db.get_recent_logs(acc["id"], limit=12)
    lines = [f"*Ultimas acoes de @{acc['username']}*\n"]
    if not logs:
        lines.append("Nenhum log ainda.")
    for log in logs:
        icon = "OK" if log.get("success") else "ERRO"
        target = f" @{log['target_username']}" if log.get("target_username") else ""
        when = (log.get("executed_at") or "")[:16]
        lines.append(f"{icon} `{log['action']}`{target} - {when}")
    return "\n".join(lines)


def _safety_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Seguranca*\n\nNenhuma conta cadastrada."
    risk = risk_detector.get_status(acc["username"])
    return (
        f"*Seguranca de @{acc['username']}*\n\n"
        f"Pausada por risco: *{'sim' if risk['is_paused'] else 'nao'}*\n"
        f"Taxa de erro: *{risk.get('error_rate', 'N/A')}*\n"
        f"Erros consecutivos: *{risk.get('consecutive_errors', 0)}*\n"
        f"Challenge detectado: *{'sim' if risk.get('challenge_detected') else 'nao'}*\n"
        f"Motivo: `{risk.get('pause_reason') or acc.get('risk_pause_reason') or 'nenhum'}`\n\n"
        "Modo seguro reduz limites e aumenta delays para diminuir risco."
    )


def _report_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    acc = _selected_account(ctx)
    if not acc:
        return "*Relatorio*\n\nNenhuma conta cadastrada."
    stats = db.get_stats_today(acc["id"])
    follows = stats.get("follow", 0)
    backs = stats.get("follow_back_detected", 0)
    rate = backs / follows * 100 if follows else 0
    return (
        f"*Relatorio rapido de @{acc['username']}*\n\n"
        f"Follows hoje: *{follows}*\n"
        f"Unfollows hoje: *{stats.get('unfollow', 0)}*\n"
        f"Follow backs hoje: *{backs}*\n"
        f"Conversao hoje: *{rate:.1f}%*\n"
        f"Erros hoje: *{stats.get('error', 0)}*\n\n"
        f"Relatorio diario automatico: *{'ativo' if acc.get('daily_report_enabled', True) else 'desativado'}*"
    )


async def _show(update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None):
    keyboard = keyboard or _back_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


def _prompt(ctx: ContextTypes.DEFAULT_TYPE, action: str):
    ctx.user_data["dashboard_pending"] = action


async def _handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    action = ctx.user_data.pop("dashboard_pending", None)
    if not action:
        return
    acc = _selected_account(ctx)
    if not acc:
        await update.message.reply_text("Nenhuma conta selecionada. Use /start.")
        return
    text = (update.message.text or "").strip()
    try:
        if action == "add_target":
            db.add_target(acc["id"], text)
            await update.message.reply_text(f"Alvo adicionado para @{acc['username']}: {text}")
        elif action == "new_campaign":
            parts = text.split("|", 1)
            name = parts[0].strip()
            niche = parts[1].strip() if len(parts) > 1 else None
            db.create_campaign(acc["id"], name, niche)
            await update.message.reply_text(f"Campanha criada: {name}")
        elif action == "add_white":
            db.add_whitelist(acc["id"], text)
            await update.message.reply_text(f"Adicionado a whitelist: {text}")
        elif action == "add_black":
            db.add_blacklist(acc["id"], text)
            await update.message.reply_text(f"Adicionado a blacklist: {text}")
        elif action == "limits":
            follows, unfollows = _parse_pair(text, "follows", "unfollows")
            accounts_db.update_settings(acc["username"], {"daily_follows": follows, "daily_unfollows": unfollows})
            await update.message.reply_text(f"Limites atualizados: {follows} follows/dia, {unfollows} unfollows/dia.")
        elif action == "hours":
            start, end = _parse_range(text)
            accounts_db.update_settings(acc["username"], {"hour_start": start, "hour_end": end})
            await update.message.reply_text(f"Horario atualizado: {start}h-{end}h.")
        elif action == "video_fundo":
            # recebe arquivo via texto (link) — para imagens enviadas como arquivo, o handler de vídeo trata
            await update.message.reply_text("Envie a imagem diretamente como foto ou arquivo no chat.")
        elif action == "video_process":
            await update.message.reply_text("Envie o .mp4 diretamente no chat para processar.")
        elif action == "video_lote":
            # coleta videos
            await update.message.reply_text("Envie os .mp4 um a um. Quando terminar, clique em 'Processar lote agora'.")
        elif action == "video_cfg_width":
            try:
                _video_user_cfg.setdefault(update.effective_user.id, {})["video_width"] = int(text)
                await update.message.reply_text(f"Largura atualizada: {text}px.")
            except ValueError:
                await update.message.reply_text("Envie um numero inteiro (ex: 800).")
        elif action == "video_cfg_pos":
            try:
                _video_user_cfg.setdefault(update.effective_user.id, {})["position_y"] = float(text)
                await update.message.reply_text(f"Posicao atualizada: {text}.")
            except ValueError:
                await update.message.reply_text("Envie um numero decimal (ex: 0.25).")
        elif action == "video_cfg_crf":
            try:
                _video_user_cfg.setdefault(update.effective_user.id, {})["output_crf"] = int(text)
                await update.message.reply_text(f"CRF atualizado: {text}.")
            except ValueError:
                await update.message.reply_text("Envie um numero inteiro (ex: 18).")
        elif action == "delay":
            delay_min, delay_max = _parse_range(text)
            accounts_db.update_settings(acc["username"], {"delay_min": delay_min, "delay_max": delay_max})
            await update.message.reply_text(f"Delay atualizado: {delay_min}-{delay_max}s.")
        elif action == "score":
            score = max(0, min(100, int(text)))
            accounts_db.update_settings(acc["username"], {"score_min": score})
            await update.message.reply_text(f"Score minimo atualizado: {score}.")
        elif action == "unfollow_days":
            days = max(0, min(365, int(text)))
            accounts_db.update_settings(acc["username"], {"unfollow_after_days": days})
            await update.message.reply_text(f"Prazo de unfollow atualizado: {days} dias.")
    except Exception as e:
        logger.error("Erro ao processar entrada guiada: %s", e, exc_info=True)
        await update.message.reply_text("Nao consegui entender/salvar. Tente novamente pelo /start.")


def _parse_range(text: str) -> tuple[int, int]:
    cleaned = text.lower().replace("h", "").replace("s", "").replace(" ", "")
    left, right = cleaned.split("-", 1)
    return int(left), int(right)


def _parse_pair(text: str, first: str, second: str) -> tuple[int, int]:
    values = {}
    for chunk in text.replace(",", " ").split():
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            values[key.strip().lower()] = int(value)
    return values[first], values[second]


@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show(update, _home_text(ctx), _main_keyboard())


@owner_only
async def on_dashboard_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _handle_pending(update, ctx)


@owner_only
async def on_dashboard_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        if data in {"dash:home", "dash:refresh"}:
            await _show(update, _home_text(ctx), _main_keyboard())
        elif data == "dash:status":
            await _show(update, _status_text(), _back_keyboard("dash:status"))
        elif data == "dash:accounts":
            await _show(update, _accounts_text(), _account_keyboard(_all_accounts()))
        elif data.startswith("dash:select:"):
            username = data.split(":", 2)[2]
            ctx.user_data["selected_account"] = username
            await _show(update, _account_text(username), _account_actions_keyboard(username))
        elif data.startswith("dash:account:"):
            await _show(update, _account_text(data.split(":", 2)[2]), _account_keyboard(_all_accounts()))
        elif data.startswith("dash:pause:"):
            username = data.split(":", 2)[2]
            accounts_db.update_status(username, "paused")
            await _show(update, f"@{username} pausada.\n\n" + _account_text(username), _account_actions_keyboard(username))
        elif data.startswith("dash:resume:"):
            username = data.split(":", 2)[2]
            accounts_db.update_status(username, "active")
            risk_detector.resume(username)
            await _show(update, f"@{username} retomada.\n\n" + _account_text(username), _account_actions_keyboard(username))
        elif data.startswith("dash:warmup:"):
            username = data.split(":", 2)[2]
            accounts_db.sb.table("ig_accounts").update({"warmup_day": 1, "status": "warming"}).eq("username", username).execute()
            await _show(update, f"Aquecimento reiniciado para @{username}.\n\n" + _account_text(username), _account_actions_keyboard(username))
        elif data == "dash:pause_all":
            accounts = accounts_db.list_active_accounts()
            for acc in accounts:
                accounts_db.update_status(acc["username"], "paused")
            await _show(update, f"{len(accounts)} conta(s) pausada(s).\n\n" + _home_text(ctx), _main_keyboard())
        elif data == "dash:resume_all":
            count = 0
            for acc in _all_accounts():
                if acc["status"] == "paused":
                    accounts_db.update_status(acc["username"], "active")
                    risk_detector.resume(acc["username"])
                    count += 1
            await _show(update, f"{count} conta(s) retomada(s).\n\n" + _home_text(ctx), _main_keyboard())
        elif data == "dash:targets":
            await _show(update, _targets_text(ctx), _targets_keyboard())
        elif data == "dash:campaigns":
            await _show(update, _campaigns_text(ctx), _campaigns_keyboard())
        elif data == "dash:config":
            await _show(update, _config_text(ctx), _config_keyboard())
        elif data == "dash:lists":
            await _show(update, _lists_text(ctx), _lists_keyboard())
        elif data == "dash:queue":
            await _show(update, _queue_text(ctx), _queue_keyboard())
        elif data == "dash:logs":
            await _show(update, _logs_text(ctx), _back_keyboard("dash:logs"))
        elif data == "dash:safety":
            await _show(update, _safety_text(ctx), InlineKeyboardMarkup([
                [_button("Ativar modo seguro", "dash:safe_mode")],
                [_button("Voltar", "dash:home"), _button("Atualizar", "dash:safety")],
            ]))
        elif data == "dash:report":
            await _show(update, _report_text(ctx), InlineKeyboardMarkup([
                [_button("Relatorio diario on/off", "dash:cfg:report_toggle")],
                [_button("Voltar", "dash:home"), _button("Atualizar", "dash:report")],
            ]))
        elif data == "dash:queue_clear":
            acc = _selected_account(ctx)
            if acc:
                ActionQueue().clear_account(acc["id"])
            await _show(update, _queue_text(ctx), _queue_keyboard())
        elif data.startswith("dash:input:"):
            action = data.rsplit(":", 1)[1]
            _prompt(ctx, action)
            prompts = {
                "add_target": "Envie o link ou @ da pagina alvo.",
                "new_campaign": "Envie `Nome da campanha | nicho`.",
                "add_white": "Envie o @usuario para whitelist.",
                "add_black": "Envie termo ou @usuario para blacklist.",
            }
            await _show(update, prompts[action], _back_keyboard())
        elif data.startswith("dash:cfg:"):
            await _handle_config_button(update, ctx, data)
        elif data.startswith("dash:set_policy:"):
            acc = _selected_account(ctx)
            policy = data.rsplit(":", 1)[1]
            accounts_db.update_settings(acc["username"], {"unfollow_policy": policy})
            await _show(update, "Regra de unfollow atualizada.\n\n" + _config_text(ctx), _config_keyboard())
        elif data == "dash:video":
            await _show(update, _video_home_text(), _video_keyboard())
        elif data == "dash:video:status":
            await _show(update, _video_status_text(), _video_keyboard())
        elif data == "dash:video:get_fundo":
            fundo = vc.ver_fundo(str(update.effective_user.id))
            if fundo:
                if update.callback_query:
                    await update.callback_query.message.reply_photo(
                        photo=fundo, caption="Fundo atual cadastrado."
                    )
                await _show(update, _video_home_text(), _video_keyboard())
            else:
                await _show(update, "*Nenhum fundo cadastrado.*\nUse o botao 'Enviar fundo'.", _video_keyboard())
        elif data == "dash:video:set_fundo":
            _prompt(ctx, "video_fundo")
            await _show(update, "Envie a imagem de fundo (PNG/JPG, 1080x1920px).", InlineKeyboardMarkup([[_button("Cancelar", "dash:video")]]))
        elif data == "dash:video:process":
            _prompt(ctx, "video_process")
            await _show(update, "Envie o video .mp4 para processar.", InlineKeyboardMarkup([[_button("Cancelar", "dash:video")]]))
        elif data == "dash:video:lote":
            ctx.user_data["video_lote"] = []
            _prompt(ctx, "video_lote")
            await _show(update, "Modo lote: envie os .mp4 um a um (max 10) e depois clique em processar.", InlineKeyboardMarkup([
                [_button("Processar lote agora", "dash:video:lote_run")],
                [_button("Cancelar", "dash:video")],
            ]))
        elif data == "dash:video:lote_run":
            lote = ctx.user_data.pop("video_lote", [])
            ctx.user_data.pop("dashboard_pending", None)
            if not lote:
                await _show(update, "Nenhum video na fila.", _video_keyboard())
                return
            if update.callback_query:
                await update.callback_query.message.reply_text(f"⏳ Processando {len(lote)} video(s)...")
            result = vc.processar_lote(lote, str(update.effective_user.id), _video_user_cfg.get(update.effective_user.id, {}))
            for item in (result.get("resultados") or []):
                if item.get("ok"):
                    vbytes = vc.download_lote_video(item["job_id"])
                    if vbytes and update.callback_query:
                        await update.callback_query.message.reply_video(
                            video=vbytes,
                            filename=f"editado_{item['arquivo']}",
                            caption=f"✅ {item['arquivo']} — {item.get('size_mb','?')} MB"
                        )
                elif update.callback_query:
                    await update.callback_query.message.reply_text(f"❌ {item['arquivo']}: {item.get('error','erro')}")
            await _show(update, _video_home_text(), _video_keyboard())
        elif data == "dash:video:config":
            uid = update.effective_user.id
            cfg = _video_user_cfg.get(uid, {})
            cfg_text = (
                "*Configuracoes do editor*\n\n"
                f"Largura do video: *{cfg.get('video_width', 800)}px*\n"
                f"Posicao vertical: *{cfg.get('position_y', 0.25)}*\n"
                f"Qualidade CRF: *{cfg.get('output_crf', 18)}*\n"
                f"Anti-ban: *{'ativo' if cfg.get('antiban', True) else 'desativado'}*\n"
                f"Fix mirror: *{'sim' if cfg.get('fix_mirror', False) else 'nao'}*"
            )
            await _show(update, cfg_text, _video_config_keyboard())
        elif data == "dash:video:config_reset":
            _video_user_cfg.pop(update.effective_user.id, None)
            await _show(update, "Configuracoes resetadas para o padrao.", _video_keyboard())
        elif data == "dash:video:limpar":
            result = vc.limpar_tmp()
            msg = f"🗑 {result.get('removidos', 0)} arquivo(s) removido(s)." if result.get("ok") else f"Erro: {result.get('error')}"
            await _show(update, msg, _video_keyboard())
        elif data.startswith("dash:video:cfg:"):
            action = data.rsplit(":", 1)[1]
            prompts_video = {
                "video_width": ("video_cfg_width", "Envie a largura em pixels (ex: 800)."),
                "position_y":  ("video_cfg_pos",   "Envie a posicao vertical (0.0=topo, 1.0=base, ex: 0.25)."),
                "output_crf":  ("video_cfg_crf",   "Envie o CRF (18=alta qualidade, 28=comprimido)."),
                "antiban":     None,
                "fix_mirror":  None,
            }
            if action in ("antiban", "fix_mirror"):
                uid = update.effective_user.id
                cfg = _video_user_cfg.setdefault(uid, {})
                cfg[action] = not cfg.get(action, action != "antiban")
                val = "ativo" if cfg[action] else "desativado"
                await _show(update, f"*{action}* atualizado: *{val}*", _video_config_keyboard())
            elif action in prompts_video:
                pkey, pmsg = prompts_video[action]
                _prompt(ctx, pkey)
                await _show(update, pmsg, InlineKeyboardMarkup([[_button("Cancelar", "dash:video:config")]]))
        elif data == "dash:safe_mode":
            acc = _selected_account(ctx)
            if acc:
                accounts_db.update_settings(acc["username"], {
                    "daily_follows": min(acc.get("daily_follows", 40), 15),
                    "daily_unfollows": min(acc.get("daily_unfollows", 40), 15),
                    "delay_min": max(acc.get("delay_min", 30), 90),
                    "delay_max": max(acc.get("delay_max", 90), 180),
                })
            await _show(update, "Modo seguro aplicado.\n\n" + _config_text(ctx), _config_keyboard())
    except Exception as e:
        logger.error("Erro no painel Telegram: %s", e, exc_info=True)
        await query.answer("Nao foi possivel executar essa acao.", show_alert=True)


async def _handle_config_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str):
    acc = _selected_account(ctx)
    if not acc:
        await _show(update, "Nenhuma conta selecionada.", _back_keyboard())
        return

    action = data.rsplit(":", 1)[1]
    if action == "limits":
        _prompt(ctx, "limits")
        await _show(update, "Envie no formato `follows=40 unfollows=30`.", _back_keyboard("dash:config"))
    elif action == "hours":
        _prompt(ctx, "hours")
        await _show(update, "Envie no formato `8-22`.", _back_keyboard("dash:config"))
    elif action == "delay":
        _prompt(ctx, "delay")
        await _show(update, "Envie no formato `30-90`.", _back_keyboard("dash:config"))
    elif action == "score":
        _prompt(ctx, "score")
        await _show(update, "Envie um score de 0 a 100.", _back_keyboard("dash:config"))
    elif action == "unfollow_days":
        _prompt(ctx, "unfollow_days")
        await _show(update, "Envie o prazo em dias. Exemplo: `5`.", _back_keyboard("dash:config"))
    elif action == "unfollow_policy":
        await _show(update, "*Escolha a regra de unfollow*", _policy_keyboard())
    elif action == "report_toggle":
        current = bool(acc.get("daily_report_enabled", True))
        accounts_db.update_settings(acc["username"], {"daily_report_enabled": not current})
        await _show(update, "Relatorio diario atualizado.\n\n" + _config_text(ctx), _config_keyboard())


def register_dashboard_handlers(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_dashboard_button, pattern=r"^dash:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_dashboard_text))
