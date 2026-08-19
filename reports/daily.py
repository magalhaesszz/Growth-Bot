import io
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from supabase import create_client

from config import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


class ReportGenerator:
    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    def _get_week_data(self, account_id: str) -> list[dict]:
        today = datetime.now(LOCAL_TZ).date()
        start_local = datetime.combine(
            today - timedelta(days=6), time.min, tzinfo=LOCAL_TZ
        )
        end_local = datetime.combine(today + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
        res = (
            self.sb.table("ig_action_logs")
            .select("action,success,executed_at")
            .eq("account_id", account_id)
            .gte("executed_at", start_local.astimezone(timezone.utc).isoformat())
            .lt("executed_at", end_local.astimezone(timezone.utc).isoformat())
            .execute()
        )
        return res.data or []

    @staticmethod
    def _local_date(log: dict):
        raw = str(log.get("executed_at", "") or "")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                LOCAL_TZ
            ).date()
        except ValueError:
            return None

    def generate_text(self, account_id: str, username: str) -> str:
        logs = self._get_week_data(account_id)
        successful = [log for log in logs if log.get("success", True)]
        follows = sum(1 for log in successful if log["action"] == "follow")
        unfollows = sum(1 for log in successful if log["action"] == "unfollow")
        follow_backs = sum(
            1 for log in successful if log["action"] == "follow_back_detected"
        )
        errors = sum(1 for log in logs if not log.get("success", True))
        conv_rate = f"{(follow_backs / follows * 100):.1f}%" if follows else "0%"

        return (
            f"📊 *Relatório semanal — @{username}*\n"
            "━━━━━━━━━━━━━━━━\n"
            f"✅ Follows realizados: *{follows}*\n"
            f"🔄 Unfollows: *{unfollows}*\n"
            f"💚 Seguiram de volta: *{follow_backs}*\n"
            f"📈 Taxa de conversão: *{conv_rate}*\n"
            f"⚠️ Erros: *{errors}*\n"
            "━━━━━━━━━━━━━━━━\n"
            f"_Gerado em {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')}_"
        )

    def generate_chart(self, account_id: str, username: str) -> io.BytesIO:
        today = datetime.now(LOCAL_TZ).date()
        days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        day_labels = [day.strftime("%d/%m") for day in days]
        logs = self._get_week_data(account_id)

        follows_per_day = []
        unfollows_per_day = []
        for day in days:
            follows_per_day.append(
                sum(
                    1
                    for log in logs
                    if log.get("success", True)
                    and log["action"] == "follow"
                    and self._local_date(log) == day
                )
            )
            unfollows_per_day.append(
                sum(
                    1
                    for log in logs
                    if log.get("success", True)
                    and log["action"] == "unfollow"
                    and self._local_date(log) == day
                )
            )

        fig, ax = plt.subplots(figsize=(8, 4))
        x = range(len(days))
        ax.bar([i - 0.2 for i in x], follows_per_day, width=0.4, label="Follows")
        ax.bar([i + 0.2 for i in x], unfollows_per_day, width=0.4, label="Unfollows")
        ax.set_xticks(list(x))
        ax.set_xticklabels(day_labels, fontsize=9)
        ax.set_title(f"@{username} — últimos 7 dias", fontsize=11)
        ax.legend(fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        plt.tight_layout()

        buffer = io.BytesIO()
        plt.savefig(buffer, format="png", dpi=120)
        buffer.seek(0)
        plt.close(fig)
        return buffer
