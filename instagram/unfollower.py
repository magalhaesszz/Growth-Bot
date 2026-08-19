import logging
import random
import time

from instagram.risk_detector import RiskDetector
from instagram.score import WhitelistFilter

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
        # O scheduler passa apenas a cota ainda disponivel naquele dia.
        self._unfollowed_in_run = 0

    def _can_unfollow(self, run_limit: int) -> bool:
        if self.risk.is_paused(self.username):
            logger.warning("[%s] Conta pausada — unfollow bloqueado.", self.username)
            return False
        if self._unfollowed_in_run >= max(0, int(run_limit)):
            logger.info("[%s] Cota restante de unfollow atingida.", self.username)
            return False
        return True

    def _follows_back(self, user_id: str) -> bool | None:
        """True/False quando confirmado; None quando a API nao conseguiu confirmar.

        Falha fechada: nunca convertemos erro de rede em permissao para unfollow.
        """
        try:
            friendship = self.cl.user_friendship(int(user_id))
            return bool(friendship.followed_by)
        except Exception as exc:
            logger.warning(
                "[%s] Nao foi possivel confirmar friendship de %s: %s",
                self.username,
                user_id,
                type(exc).__name__,
            )
            return None

    def auto_unfollow_follow_backs(
        self,
        account_id: str,
        db,
        daily_limit: int,
        delay_min: int,
        delay_max: int,
        max_checks: int = 50,
    ) -> int:
        """Remove follow-backs confirmados, respeitando whitelist e cota restante."""
        if daily_limit <= 0:
            return 0
        try:
            following = db.get_following_list(account_id, limit=max_checks)
        except Exception as exc:
            logger.error("Erro ao buscar following: %s", exc)
            return 0

        count = 0
        checks = 0
        for entry in following:
            if not self._can_unfollow(daily_limit) or checks >= max_checks:
                break
            uid = str(entry.get("target_user_id", "") or "")
            uname = str(entry.get("target_username", "") or "")
            if not uid or not uname:
                continue
            if self.whitelist.is_protected(uname):
                continue

            checks += 1
            follows_back = self._follows_back(uid)
            if follows_back is not True:
                # False = nao segue de volta; None = nao foi possivel confirmar.
                continue

            try:
                self.cl.user_unfollow(int(uid))
                db.mark_unfollowed(account_id, uname)
                db.mark_follows_back(account_id, uname)
                db.log_action(account_id, "unfollow", uname, "auto_follow_back", True)
                self._unfollowed_in_run += 1
                count += 1
                self.risk.record_success(self.username)
                self.client.save_session()
                logger.info(
                    "[%s] Auto-unfollow @%s (follow-back confirmado)",
                    self.username,
                    uname,
                )
                time.sleep(random.uniform(delay_min, delay_max))
            except Exception as exc:
                db.log_action(
                    account_id, "unfollow", uname, "auto_follow_back", False
                )
                err_str = str(exc).lower()
                if "login_required" in err_str or "loginrequired" in err_str:
                    self.risk.notify_session_expired(self.username)
                    break
                self.risk.record_error(self.username, exc)
                logger.error(
                    "[%s] Erro auto-unfollow @%s: %s", self.username, uname, exc
                )
                if self.risk.is_paused(self.username):
                    break
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
        if daily_limit <= 0:
            return results

        for entry in candidates:
            if not self._can_unfollow(daily_limit):
                break

            uname = str(
                entry.get("target_username") or entry.get("username", "") or ""
            )
            uid = str(entry.get("target_user_id") or entry.get("user_id", "") or "")
            if not uname or not uid or not uid.isdigit():
                results["errors"] += 1
                logger.error(
                    "Candidato de unfollow sem username/user_id valido: %s",
                    entry.get("id"),
                )
                continue

            if self.whitelist.is_protected(uname):
                results["kept"] += 1
                continue

            follows_back = self._follows_back(uid)
            if follows_back is None:
                # Nunca faca unfollow quando a relacao nao pôde ser confirmada.
                results["skipped"] += 1
                continue

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
                self._unfollowed_in_run += 1
                results["unfollowed"] += 1
                self.risk.record_success(self.username)
                self.client.save_session()
                logger.info("[%s] Deixou de seguir @%s", self.username, uname)

                if on_success:
                    on_success(uname, uid, False)
                time.sleep(random.uniform(delay_min, delay_max))

            except Exception as exc:
                results["errors"] += 1
                err_str = str(exc).lower()
                if "login_required" in err_str or "loginrequired" in err_str:
                    self.risk.notify_session_expired(self.username)
                    break
                self.risk.record_error(self.username, exc)
                logger.error(
                    "[%s] Erro ao deixar de seguir @%s: %s",
                    self.username,
                    uname,
                    exc,
                )
                if self.risk.is_paused(self.username):
                    break
                time.sleep(random.uniform(10, 30))

        logger.info(
            "[%s] Unfollow batch — removidos=%s mantidos=%s pulados=%s erros=%s",
            self.username,
            results["unfollowed"],
            results["kept"],
            results["skipped"],
            results["errors"],
        )
        return results
