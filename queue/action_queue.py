import json
import logging
import time
from datetime import datetime, timedelta

from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY, QUEUE_MAX_RETRIES, QUEUE_BACKOFF_BASE

logger = logging.getLogger(__name__)


class ActionQueue:
    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    def enqueue(self, account_id: str, action: str, payload: dict) -> dict:
        row = {
            "account_id": account_id,
            "action": action,
            "payload": payload,
            "status": "pending",
            "retries": 0,
            "next_attempt_at": datetime.utcnow().isoformat(),
        }
        res = self.sb.table("ig_action_queue").insert(row).execute()
        return res.data[0] if res.data else {}

    def get_pending(self, account_id: str) -> list[dict]:
        now = datetime.utcnow().isoformat()
        res = (
            self.sb.table("ig_action_queue")
            .select("*")
            .eq("account_id", account_id)
            .eq("status", "pending")
            .lte("next_attempt_at", now)
            .order("created_at")
            .limit(20)
            .execute()
        )
        return res.data or []

    def mark_done(self, queue_id: str):
        self.sb.table("ig_action_queue").update({"status": "done"}).eq("id", queue_id).execute()

    def mark_retry(self, queue_id: str, current_retries: int):
        if current_retries >= QUEUE_MAX_RETRIES:
            self.sb.table("ig_action_queue").update({"status": "failed"}).eq("id", queue_id).execute()
            logger.warning(f"Ação {queue_id} falhou após {current_retries} tentativas.")
            return

        backoff = QUEUE_BACKOFF_BASE * (2 ** current_retries)
        next_attempt = (datetime.utcnow() + timedelta(seconds=backoff)).isoformat()
        self.sb.table("ig_action_queue").update({
            "retries": current_retries + 1,
            "next_attempt_at": next_attempt,
        }).eq("id", queue_id).execute()
        logger.info(f"Ação {queue_id} reagendada em {backoff}s (tentativa {current_retries + 1})")

    def clear_account(self, account_id: str):
        self.sb.table("ig_action_queue").delete().eq("account_id", account_id).eq("status", "pending").execute()

    def list_pending(self, account_id: str) -> list[dict]:
        res = (
            self.sb.table("ig_action_queue")
            .select("id, action, retries, next_attempt_at, status")
            .eq("account_id", account_id)
            .neq("status", "done")
            .order("created_at")
            .execute()
        )
        return res.data or []
