import time
import random
import logging
from datetime import date

from instagram.score import WhitelistFilter
from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)


class Unfollower:
    def __init__(
        self,
        ig_client,
        risk_detector: RiskDetector,
        whitelist: WhitelistFilter = None,
    ):
        self.cl = ig_client.api
        self.client = ig_client
        self.username = ig_client.username
        self.risk = risk_detector
        self.whitelist = whitelist or WhitelistFilter([])
        self._unfollowed_today = 0
        self._today = date.today()

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._today:
            self._unfollowed_today = 0
            self._today = today

    def _can_unfollow(self, daily_limit: int) -> bool:
        self._reset_if_new_day()
        if self.risk.is_paused(self.username):
            logger.warning(f"[{self.username}] Conta pausada — unfollow bloqueado.")
            return False
        if self._unfollowed_today >= daily_limit:
            logger.info(f"[{self.username}] Limite diário de unfollow atingido.")
            return False
        return True

    def _follows_back(self, user_id: str) -> bool:
        try:
            friendship = self.cl.user_friendship(int(user_id))
            return friendship.followed_by
        except Exception as e:
            logger.warning(f"Erro ao checar friendship de {user_id}: {e}")
            return True  # em caso de dúvida, mantém o follow

    def auto_unfollow_follow_backs(
        self,
        account_id: str,
        db,
        daily_limit: int,
        delay_min: int,
        delay_max: int,
    ) -> int:
        """
        Checa quem seguiu de volta e faz unfollow automatico.
        Retorna quantos foram removidos.
        """
        count = 0
        try:
            following = db.get_following_list(account_id, limit=500)
        except Exception as e:
            logger.error(f"Erro ao buscar following: {e}")
            return 0

        for entry in following:
            if not self._can_unfollow(daily_limit):
                break
            uid   = entry.get("target_user_id", "")
            uname = entry.get("target_username", "")
            if not uid or not uname:
                continue
            # Checar se segue de volta agora
            if self._follows_back(uid):
                try:
                    self.cl.user_unfollow(int(uid))
                    db.mark_unfollowed(account_id, uname)
                    db.mark_follows_back(account_id, uname)
                    db.log_action(account_id, "unfollow", uname, "auto_follow_back", True)
                    self._unfollowed_today += 1
                    self.client.save_session()
                    count += 1
                    logger.info(f"[{self.username}] Auto-unfollow @{uname} (seguiu de volta) ✓")
                    time.sleep(random.uniform(delay_min, delay_max))
                except Exception as e:
                    logger.error(f"[{self.username}] Erro auto-unfollow @{uname}: {e}")
                    time.sleep(random.uniform(10, 20))
        return count

    def unfollow_batch(
        self,
        candidates: list[dict],
        daily_limit: int,
        delay_min: int,
        delay_max: int,
        on_success=None,
        policy: str = "keep_follow_backs",
    ) -> dict:
        results = {"unfollowed": 0, "kept": 0, "skipped": 0, "errors": 0}

        for entry in candidates:
            if not self._can_unfollow(daily_limit):
                break

            uname = entry.get("target_username") or entry.get("username", "")
            uid = entry.get("target_user_id") or entry.get("user_id", "")

            if not uname or not uid:
                results["errors"] += 1
                logger.error("Candidato de unfollow sem username/user_id: %s", entry.get("id"))
                continue

            if self.whitelist.is_protected(uname):
                results["kept"] += 1
                logger.debug(f"@{uname} na whitelist — mantendo.")
                continue

            follows_back = self._follows_back(uid)
            keep = (
                (policy == "keep_follow_backs" and follows_back)
                or (policy == "remove_only_follow_backs" and not follows_back)
            )
            if keep:
                results["kept"] += 1
                if on_success:
                    on_success(uname, uid, True)
                continue

            try:
                self.cl.user_unfollow(int(uid))
                self._unfollowed_today += 1
                results["unfollowed"] += 1
                self.risk.record_success(self.username)
                self.client.save_session()
                logger.info(f"[{self.username}] Deixou de seguir @{uname} ✓")

                if on_success:
                    on_success(uname, uid, False)

                time.sleep(random.uniform(delay_min, delay_max))

            except Exception as e:
                results["errors"] += 1
                self.risk.record_error(self.username, e)
                logger.error(f"[{self.username}] Erro ao deixar de seguir @{uname}: {e}")

                if self.risk.is_paused(self.username):
                    break

                time.sleep(random.uniform(10, 30))

        logger.info(
            f"[{self.username}] Unfollow batch — "
            f"removidos: {results['unfollowed']}, mantidos: {results['kept']}, erros: {results['errors']}"
        )
        return results
