import logging
from datetime import datetime
from database.accounts import AccountsDB
from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)

accounts_db = AccountsDB()


async def check_all_anomalies(risk_detector: RiskDetector, notify_fn=None):
    """
    Verifica anomalias em todas as contas ativas.
    Chame a cada 30 minutos pelo scheduler.
    notify_fn: corrotina para enviar alerta no Telegram.
    """
    accounts = accounts_db.list_active_accounts()

    for acc in accounts:
        username = acc["username"]
        hour_start = acc.get("hour_start", 8)
        hour_end = acc.get("hour_end", 22)

        # Anomalia: bot deveria agir mas não agiu
        is_anomaly = risk_detector.check_anomaly(username, hour_start, hour_end)
        if is_anomaly and notify_fn:
            await notify_fn(
                f"⚠️ *Anomalia detectada — @{username}*\n"
                f"Sem ações há mais de 2h dentro da janela operacional ({hour_start}h-{hour_end}h).\n"
                f"Use /status para verificar."
            )

        # Conta pausada pelo detector de risco — notifica
        if risk_detector.is_paused(username):
            status = risk_detector.get_status(username)
            challenge = status.get("challenge_detected", False)
            reason = status.get("pause_reason", "motivo desconhecido")

            if notify_fn:
                msg = (
                    f"🔴 *Conta pausada — @{username}*\n"
                    f"Motivo: {reason}\n"
                )
                if challenge:
                    msg += "⚠️ *Desafio de segurança detectado!* Acesse o Instagram manualmente."
                msg += "\nUse /retomar após verificar."
                await notify_fn(msg)
