import logging
from datetime import datetime
from database.accounts import AccountsDB
from config import WARMUP_SCHEDULE

logger = logging.getLogger(__name__)

accounts_db = AccountsDB()


def get_warmup_limit(warmup_day: int) -> int:
    """Retorna o limite de follows para o dia de aquecimento atual."""
    if warmup_day <= 0:
        return 0
    idx = min(warmup_day - 1, len(WARMUP_SCHEDULE) - 1)
    return WARMUP_SCHEDULE[idx]


def is_warmup_complete(warmup_day: int) -> bool:
    return warmup_day > len(WARMUP_SCHEDULE)


async def advance_all_warmups(notify_fn=None):
    """
    Avança o dia de aquecimento de todas as contas em warming.
    Chame uma vez por dia pelo scheduler.
    notify_fn: corrotina opcional para enviar mensagem no Telegram.
    """
    accounts = accounts_db.list_active_accounts()
    for acc in accounts:
        warmup_day = acc.get("warmup_day", 0)
        if warmup_day <= 0:
            continue

        username = acc["username"]
        next_day = accounts_db.advance_warmup_day(username)
        today_limit = get_warmup_limit(next_day)

        logger.info(f"[{username}] Aquecimento dia {next_day} — limite hoje: {today_limit} follows")

        if is_warmup_complete(next_day):
            accounts_db.finish_warmup(username)
            logger.info(f"[{username}] Aquecimento concluído! Conta em modo ativo.")
            if notify_fn:
                await notify_fn(
                    f"🔥 Aquecimento de *@{username}* concluído!\n"
                    f"Conta ativada com limite completo de {acc.get('daily_follows', 40)} follows/dia."
                )
        else:
            if notify_fn:
                await notify_fn(
                    f"🌡 *@{username}* — aquecimento dia {next_day}/{len(WARMUP_SCHEDULE)}\n"
                    f"Limite de hoje: {today_limit} follows"
                )
