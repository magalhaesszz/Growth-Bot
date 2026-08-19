import logging

from database.accounts import AccountsDB
from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)

accounts_db = AccountsDB()


async def check_all_anomalies(risk_detector: RiskDetector, notify_fn=None):
    """
    Verifica anomalias em todas as contas ativas.
    Chame a cada 30 minutos pelo scheduler.

    As pausas de risco ja sao notificadas pelo proprio RiskDetector no momento
    em que acontecem. Este job nao repete esse alerta enquanto a conta continuar
    pausada, evitando spam a cada execucao do scheduler.
    """
    accounts = accounts_db.list_active_accounts()

    for acc in accounts:
        username = acc["username"]
        hour_start = acc.get("hour_start", 8)
        hour_end = acc.get("hour_end", 22)

        # Anomalia: bot deveria agir mas nao agiu
        is_anomaly = risk_detector.check_anomaly(username, hour_start, hour_end)
        if is_anomaly and notify_fn:
            await notify_fn(
                f"⚠️ *Anomalia detectada — @{username}*\n"
                f"Sem ações há mais de 2h dentro da janela operacional ({hour_start}h-{hour_end}h).\n"
                f"Use /status para verificar."
            )
