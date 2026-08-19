import logging
import random
import time

from instagram.risk_detector import RiskDetector
from instagram.score import BlacklistFilter, ProfileScorer

logger = logging.getLogger(__name__)


def _sleep(seconds: float, stop_event=None) -> bool:
    """Espera e retorna False quando uma parada cooperativa foi solicitada."""
    if stop_event is not None:
        return not stop_event.wait(max(0.0, seconds))
    time.sleep(max(0.0, seconds))
    return True


class Follower:
    def __init__(
        self,
        ig_client,
        risk_detector: RiskDetector,
        scorer: ProfileScorer = None,
        blacklist: BlacklistFilter = None,
    ):
        self.cl = ig_client.api
        self.client = ig_client
        self.username = ig_client.username
        self.risk = risk_detector
        self.scorer = scorer or ProfileScorer()
        self.blacklist = blacklist or BlacklistFilter([])
        # O scheduler calcula a cota restante no banco. Este contador vale para
        # toda a instancia e impede dupla contagem entre varios alvos do job.
        self._followed_in_run = 0

    def _can_follow(self, run_limit: int, stop_event=None) -> bool:
        if stop_event is not None and stop_event.is_set():
            return False
        if self.risk.is_paused(self.username):
            logger.warning("[%s] Conta pausada — follow bloqueado.", self.username)
            return False
        if self._followed_in_run >= max(0, int(run_limit)):
            logger.info("[%s] Cota restante do job atingida.", self.username)
            return False
        return True

    def follow_batch(
        self,
        profiles: list[dict],
        daily_limit: int,
        min_score: int,
        delay_min: int,
        delay_max: int,
        on_success=None,
        stop_event=None,
    ) -> dict:
        """Segue perfis respeitando a cota restante calculada pelo scheduler."""
        results = {"followed": 0, "skipped": 0, "errors": 0, "stopped": False}

        for profile in profiles:
            if not self._can_follow(daily_limit, stop_event):
                results["stopped"] = bool(stop_event and stop_event.is_set())
                break

            uname = str(profile.get("username", "") or "")
            uid = str(profile.get("user_id", "") or "")
            if not uname or not uid or not uid.isdigit():
                results["skipped"] += 1
                continue

            if self.blacklist.is_blocked(profile):
                results["skipped"] += 1
                continue
            if not self.scorer.passes(profile, min_score):
                results["skipped"] += 1
                continue

            try:
                self.cl.user_follow(int(uid))
                self._followed_in_run += 1
                results["followed"] += 1
                self.risk.record_success(self.username)
                self.client.save_session()
                logger.info("[%s] Seguiu @%s", self.username, uname)

                if on_success:
                    on_success(uname, uid)

                if not _sleep(random.uniform(delay_min, delay_max), stop_event):
                    results["stopped"] = True
                    break

            except Exception as exc:
                results["errors"] += 1
                err_str = str(exc).lower()

                if any(
                    marker in err_str
                    for marker in ("login_required", "loginrequired", "sessionid")
                ):
                    self.risk.notify_session_expired(self.username)
                    break

                self.risk.record_error(self.username, exc)
                logger.error("[%s] Erro ao seguir @%s: %s", self.username, uname, exc)

                if self.risk.is_paused(self.username):
                    break
                if not _sleep(random.uniform(10, 30), stop_event):
                    results["stopped"] = True
                    break

        logger.info(
            "[%s] Batch concluido — seguidos=%s pulados=%s erros=%s",
            self.username,
            results["followed"],
            results["skipped"],
            results["errors"],
        )
        return results
