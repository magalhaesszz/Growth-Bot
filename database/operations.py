import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase import create_client

from config import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _local_day_bounds(now: datetime | None = None) -> tuple[str, str]:
    """Inicio/fim do dia de Sao Paulo convertidos para UTC (TIMESTAMPTZ)."""
    local_now = now.astimezone(LOCAL_TZ) if now else datetime.now(LOCAL_TZ)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _story_count(detail) -> int:
    if detail is None:
        return 1
    text = str(detail)
    match = re.search(r"(\d+)", text)
    if not match:
        return 1
    try:
        return max(0, int(match.group(1)))
    except ValueError:
        return 1


class DB:
    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ─── Seguidos ────────────────────────────────────────────

    def add_followed(self, account_id, user_id, username, campaign_id=None, score=None):
        user_id = str(user_id)
        existing = (
            self.sb.table("ig_followed")
            .select("id")
            .eq("account_id", account_id)
            .eq("target_user_id", user_id)
            .limit(1)
            .execute()
        )
        payload = {
            "target_username": username,
            "campaign_id": campaign_id,
            "score": score,
            "status": "following",
            "followed_at": datetime.now(timezone.utc).isoformat(),
            "unfollowed_at": None,
            "follows_back": False,
        }
        if existing.data:
            self.sb.table("ig_followed").update(payload).eq(
                "id", existing.data[0]["id"]
            ).execute()
            return
        self.sb.table("ig_followed").insert(
            {"account_id": account_id, "target_user_id": user_id, **payload}
        ).execute()

    def get_unfollow_candidates(self, account_id, after_days: int) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).isoformat()
        res = (
            self.sb.table("ig_followed")
            .select("*")
            .eq("account_id", account_id)
            .eq("status", "following")
            .lte("followed_at", cutoff)
            .execute()
        )
        return res.data or []

    def mark_unfollowed(self, account_id, target_username):
        self.sb.table("ig_followed").update(
            {
                "status": "unfollowed",
                "unfollowed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("account_id", account_id).eq(
            "target_username", target_username
        ).execute()

    def mark_follows_back(self, account_id, target_username):
        self.sb.table("ig_followed").update({"follows_back": True}).eq(
            "account_id", account_id
        ).eq("target_username", target_username).execute()

    def get_already_following_ids(self, account_id) -> set[str]:
        res = (
            self.sb.table("ig_followed")
            .select("target_user_id")
            .eq("account_id", account_id)
            .eq("status", "following")
            .execute()
        )
        return {str(row["target_user_id"]) for row in (res.data or [])}

    def _count_today_log_action(self, account_id, action: str) -> int:
        start, end = _local_day_bounds()
        res = (
            self.sb.table("ig_action_logs")
            .select("id", count="exact")
            .eq("account_id", account_id)
            .eq("action", action)
            .eq("success", True)
            .gte("executed_at", start)
            .lt("executed_at", end)
            .execute()
        )
        return res.count or 0

    def count_today_follows(self, account_id) -> int:
        return self._count_today_log_action(account_id, "follow")

    def count_today_unfollows(self, account_id) -> int:
        return self._count_today_log_action(account_id, "unfollow")

    # ─── Alvos ───────────────────────────────────────────────

    def add_target(
        self, account_id, page_url, page_username=None, page_user_id=None, campaign_id=None
    ):
        self.sb.table("ig_targets").insert(
            {
                "account_id": account_id,
                "page_url": page_url,
                "page_username": page_username,
                "page_user_id": page_user_id,
                "campaign_id": campaign_id,
            }
        ).execute()

    def list_targets(self, account_id) -> list[dict]:
        res = (
            self.sb.table("ig_targets")
            .select("*")
            .eq("account_id", account_id)
            .eq("status", "active")
            .order("priority")
            .execute()
        )
        return res.data or []

    def remove_target(self, account_id, page_username):
        self.sb.table("ig_targets").update({"status": "removed"}).eq(
            "account_id", account_id
        ).eq("page_username", page_username).execute()

    def update_target_scraped(self, target_id, count: int):
        count = max(0, int(count))
        current = (
            self.sb.table("ig_targets")
            .select("scraped_count")
            .eq("id", target_id)
            .limit(1)
            .execute()
        )
        previous = int(current.data[0].get("scraped_count", 0) or 0) if current.data else 0
        self.sb.table("ig_targets").update(
            {
                "scraped_count": previous + count,
                "last_scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", target_id).execute()

    # ─── Campanhas ───────────────────────────────────────────

    def create_campaign(self, account_id, name, nicho=None) -> dict:
        res = self.sb.table("ig_campaigns").insert(
            {"account_id": account_id, "name": name, "nicho": nicho}
        ).execute()
        return res.data[0] if res.data else {}

    def get_active_campaign(self, account_id) -> dict | None:
        res = (
            self.sb.table("ig_campaigns")
            .select("*")
            .eq("account_id", account_id)
            .eq("status", "active")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def list_campaigns(self, account_id) -> list[dict]:
        res = (
            self.sb.table("ig_campaigns")
            .select("*")
            .eq("account_id", account_id)
            .order("started_at", desc=True)
            .execute()
        )
        return res.data or []

    def update_campaign_stats(self, campaign_id, follows=0, unfollows=0, follow_backs=0):
        # As operacoes de uma mesma conta sao serializadas no scheduler. Isso
        # evita o read/modify/write concorrente sem exigir uma RPC especifica.
        camp = (
            self.sb.table("ig_campaigns")
            .select("total_follows,total_unfollows,total_follow_backs")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        if not camp.data:
            return
        current = camp.data[0]
        self.sb.table("ig_campaigns").update(
            {
                "total_follows": int(current.get("total_follows", 0) or 0) + follows,
                "total_unfollows": int(current.get("total_unfollows", 0) or 0)
                + unfollows,
                "total_follow_backs": int(current.get("total_follow_backs", 0) or 0)
                + follow_backs,
            }
        ).eq("id", campaign_id).execute()

    # ─── Whitelist / Blacklist ───────────────────────────────

    def add_whitelist(self, account_id, username):
        username = username.lower().lstrip("@")
        existing = (
            self.sb.table("ig_whitelist")
            .select("id")
            .eq("account_id", account_id)
            .eq("target_username", username)
            .limit(1)
            .execute()
        )
        if not existing.data:
            self.sb.table("ig_whitelist").insert(
                {"account_id": account_id, "target_username": username}
            ).execute()

    def remove_whitelist(self, account_id, username):
        self.sb.table("ig_whitelist").delete().eq("account_id", account_id).eq(
            "target_username", username.lower().lstrip("@")
        ).execute()

    def get_whitelist(self, account_id) -> list[str]:
        res = (
            self.sb.table("ig_whitelist")
            .select("target_username")
            .eq("account_id", account_id)
            .execute()
        )
        return [row["target_username"] for row in (res.data or [])]

    def add_blacklist(self, account_id, term, kind="username"):
        term = term.lower().lstrip("@")
        existing = (
            self.sb.table("ig_blacklist")
            .select("id")
            .eq("account_id", account_id)
            .eq("term", term)
            .eq("type", kind)
            .limit(1)
            .execute()
        )
        if not existing.data:
            self.sb.table("ig_blacklist").insert(
                {"account_id": account_id, "term": term, "type": kind}
            ).execute()

    def get_blacklist(self, account_id) -> list[str]:
        res = (
            self.sb.table("ig_blacklist")
            .select("term")
            .eq("account_id", account_id)
            .execute()
        )
        return [row["term"] for row in (res.data or [])]

    # ─── Logs ────────────────────────────────────────────────

    def log_action(
        self, account_id, action, target_username=None, detail=None, success=True
    ):
        self.sb.table("ig_action_logs").insert(
            {
                "account_id": account_id,
                "action": action,
                "target_username": target_username,
                "detail": detail,
                "success": bool(success),
            }
        ).execute()

    def get_recent_logs(self, account_id, limit=20) -> list[dict]:
        res = (
            self.sb.table("ig_action_logs")
            .select("*")
            .eq("account_id", account_id)
            .order("executed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def get_following_list(self, account_id, limit: int = 200) -> list[dict]:
        res = (
            self.sb.table("ig_followed")
            .select("target_username,target_user_id,followed_at,follows_back,status")
            .eq("account_id", account_id)
            .eq("status", "following")
            .order("followed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def get_non_followers(self, account_id, limit: int = 0) -> list[dict]:
        """Registros ainda nao confirmados como follow-back no banco local."""
        query = (
            self.sb.table("ig_followed")
            .select("target_username,target_user_id,followed_at,follows_back")
            .eq("account_id", account_id)
            .eq("status", "following")
            .eq("follows_back", False)
            .order("followed_at", desc=True)
        )
        if limit > 0:
            query = query.limit(limit)
        return query.execute().data or []

    def unfollow_user_by_username(self, account_id, username: str):
        self.mark_unfollowed(account_id, username)

    def check_and_mark_follow_back(self, account_id, user_id: str, username: str) -> bool:
        res = (
            self.sb.table("ig_followed")
            .select("follows_back")
            .eq("account_id", account_id)
            .eq("target_user_id", str(user_id))
            .eq("status", "following")
            .limit(1)
            .execute()
        )
        return bool(res.data and res.data[0].get("follows_back", False))

    def get_stats_today(self, account_id) -> dict:
        start, end = _local_day_bounds()
        logs = (
            self.sb.table("ig_action_logs")
            .select("action,success,detail")
            .eq("account_id", account_id)
            .gte("executed_at", start)
            .lt("executed_at", end)
            .execute()
        ).data or []

        stats = {
            "follow": 0,
            "unfollow": 0,
            "story_view": 0,
            "follow_back_detected": 0,
            "error": 0,
        }
        for log in logs:
            if not log.get("success", True):
                stats["error"] += 1
                continue
            action = log.get("action")
            if action == "story_view":
                stats["story_view"] += _story_count(log.get("detail"))
            elif action in stats:
                stats[action] += 1
        return stats
