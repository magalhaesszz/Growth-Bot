import io
import logging
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)


class ReportGenerator:
    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    def _get_week_data(self, account_id: str) -> list[dict]:
        since = (datetime.utcnow() - timedelta(days=7)).isoformat()
        res = (
            self.sb.table("ig_action_logs")
            .select("action, success, executed_at")
            .eq("account_id", account_id)
            .gte("executed_at", since)
            .execute()
        )
        return res.data or []

    def generate_text(self, account_id: str, username: str) -> str:
        logs = self._get_week_data(account_id)
        follows = sum(1 for l in logs if l["action"] == "follow" and l["success"])
        unfollows = sum(1 for l in logs if l["action"] == "unfollow" and l["success"])
        follow_backs = sum(
            1 for l in logs if l["action"] == "follow_back_detected" and l["success"]
        )
        errors = sum(1 for l in logs if not l["success"])

        conv_rate = f"{(follow_backs / follows * 100):.1f}%" if follows else "0%"

        text = (
            f"📊 *Relatório semanal — @{username}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ Follows realizados: *{follows}*\n"
            f"🔄 Unfollows: *{unfollows}*\n"
            f"💚 Seguiram de volta: *{follow_backs}*\n"
            f"📈 Taxa de conversão: *{conv_rate}*\n"
            f"⚠️ Erros: *{errors}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"_Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
        )
        return text

    def generate_chart(self, account_id: str, username: str) -> io.BytesIO:
        """Gera gráfico de barras dos últimos 7 dias. Retorna buffer PNG."""
        since = datetime.utcnow() - timedelta(days=6)
        days = [(since + timedelta(days=i)).date() for i in range(7)]
        day_labels = [d.strftime("%d/%m") for d in days]

        logs = self._get_week_data(account_id)

        follows_per_day = []
        unfollows_per_day = []
        for d in days:
            d_str = d.isoformat()
            follows_per_day.append(sum(
                1 for l in logs
                if l["action"] == "follow" and l["success"] and l["executed_at"][:10] == d_str
            ))
            unfollows_per_day.append(sum(
                1 for l in logs
                if l["action"] == "unfollow" and l["success"] and l["executed_at"][:10] == d_str
            ))

        fig, ax = plt.subplots(figsize=(8, 4))
        x = range(len(days))
        ax.bar([i - 0.2 for i in x], follows_per_day, width=0.4, label="Follows", color="#1D9E75")
        ax.bar([i + 0.2 for i in x], unfollows_per_day, width=0.4, label="Unfollows", color="#D85A30")
        ax.set_xticks(list(x))
        ax.set_xticklabels(day_labels, fontsize=9)
        ax.set_title(f"@{username} — últimos 7 dias", fontsize=11)
        ax.legend(fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120)
        buf.seek(0)
        plt.close(fig)
        return buf
