import logging
from datetime import datetime, timedelta
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)


class DB:
    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ─── Seguidos ────────────────────────────────────────────

    def add_followed(self, account_id, user_id, username, campaign_id=None, score=None):
        self.sb.table("ig_followed").insert({
            "account_id": account_id,
            "target_user_id": user_id,
            "target_username": username,
            "campaign_id": campaign_id,
            "score": score,
            "status": "following",
        }).execute()

    def get_unfollow_candidates(self, account_id, after_days: int) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=after_days)).isoformat()
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
        self.sb.table("ig_followed").update({
            "status": "unfollowed",
            "unfollowed_at": datetime.utcnow().isoformat(),
        }).eq("account_id", account_id).eq("target_username", target_username).execute()

    def mark_follows_back(self, account_id, target_username):
        self.sb.table("ig_followed").update({"follows_back": True}).eq(
            "account_id", account_id).eq("target_username", target_username).execute()

    def get_already_following_ids(self, account_id) -> set:
        res = (
            self.sb.table("ig_followed")
            .select("target_user_id")
            .eq("account_id", account_id)
            .eq("status", "following")
            .execute()
        )
        return {r["target_user_id"] for r in (res.data or [])}

    def count_today_follows(self, account_id) -> int:
        today = datetime.utcnow().date().isoformat()
        res = (
            self.sb.table("ig_followed")
            .select("id", count="exact")
            .eq("account_id", account_id)
            .gte("followed_at", today)
            .execute()
        )
        return res.count or 0

    # ─── Alvos ───────────────────────────────────────────────

    def add_target(self, account_id, page_url, page_username=None, page_user_id=None, campaign_id=None):
        self.sb.table("ig_targets").insert({
            "account_id": account_id,
            "page_url": page_url,
            "page_username": page_username,
            "page_user_id": page_user_id,
            "campaign_id": campaign_id,
        }).execute()

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
            "account_id", account_id).eq("page_username", page_username).execute()

    def update_target_scraped(self, target_id, count: int):
        self.sb.table("ig_targets").update({
            "scraped_count": count,
            "last_scraped_at": datetime.utcnow().isoformat(),
        }).eq("id", target_id).execute()

    # ─── Campanhas ───────────────────────────────────────────

    def create_campaign(self, account_id, name, nicho=None) -> dict:
        res = self.sb.table("ig_campaigns").insert({
            "account_id": account_id,
            "name": name,
            "nicho": nicho,
        }).execute()
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
        camp = self.sb.table("ig_campaigns").select("*").eq("id", campaign_id).execute()
        if not camp.data:
            return
        c = camp.data[0]
        self.sb.table("ig_campaigns").update({
            "total_follows": c["total_follows"] + follows,
            "total_unfollows": c["total_unfollows"] + unfollows,
            "total_follow_backs": c["total_follow_backs"] + follow_backs,
        }).eq("id", campaign_id).execute()

    # ─── Whitelist / Blacklist ────────────────────────────────

    def add_whitelist(self, account_id, username):
        self.sb.table("ig_whitelist").upsert({
            "account_id": account_id,
            "target_username": username.lstrip("@"),
        }).execute()

    def remove_whitelist(self, account_id, username):
        self.sb.table("ig_whitelist").delete().eq(
            "account_id", account_id).eq("target_username", username.lstrip("@")).execute()

    def get_whitelist(self, account_id) -> list[str]:
        res = self.sb.table("ig_whitelist").select("target_username").eq("account_id", account_id).execute()
        return [r["target_username"] for r in (res.data or [])]

    def add_blacklist(self, account_id, term, kind="username"):
        self.sb.table("ig_blacklist").upsert({
            "account_id": account_id,
            "term": term.lower().lstrip("@"),
            "type": kind,
        }).execute()

    def get_blacklist(self, account_id) -> list[str]:
        res = self.sb.table("ig_blacklist").select("term").eq("account_id", account_id).execute()
        return [r["term"] for r in (res.data or [])]

    # ─── Logs ────────────────────────────────────────────────

    def log_action(self, account_id, action, target_username=None, detail=None, success=True):
        self.sb.table("ig_action_logs").insert({
            "account_id": account_id,
            "action": action,
            "target_username": target_username,
            "detail": detail,
            "success": success,
        }).execute()

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

    def get_stats_today(self, account_id) -> dict:
        today = datetime.utcnow().date().isoformat()
        logs = (
            self.sb.table("ig_action_logs")
            .select("action, success")
            .eq("account_id", account_id)
            .gte("executed_at", today)
            .execute()
        ).data or []

        stats = {"follow": 0, "unfollow": 0, "story_view": 0, "error": 0}
        for log in logs:
            action = log["action"]
            if action in stats:
                stats[action] += 1
            elif not log["success"]:
                stats["error"] += 1
        return stats
