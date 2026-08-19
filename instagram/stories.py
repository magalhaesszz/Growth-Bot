import logging
import random
import time

from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)


class StoriesViewer:
    def __init__(self, ig_client, risk_detector: RiskDetector):
        self.cl = ig_client.api
        self.client = ig_client
        self.username = ig_client.username
        self.risk = risk_detector

    def view_stories_for_users(
        self,
        user_ids: list[str],
        max_per_run: int = 30,
        delay_min: float = 5.0,
        delay_max: float = 15.0,
        stop_event=None,
    ) -> dict:
        results = {"viewed": 0, "no_story": 0, "errors": 0, "stopped": False}

        for uid in user_ids[:max_per_run]:
            if (stop_event is not None and stop_event.is_set()) or self.risk.is_paused(
                self.username
            ):
                results["stopped"] = bool(stop_event and stop_event.is_set())
                break

            try:
                stories = self.cl.user_stories(int(uid))
                if not stories:
                    results["no_story"] += 1
                    continue

                story_ids = [story.pk for story in stories]
                self.cl.story_seen(story_ids)
                results["viewed"] += len(story_ids)
                self.risk.record_success(self.username)
                logger.debug("[%s] Stories vistos: user %s", self.username, uid)

                delay = random.uniform(delay_min, delay_max)
                if stop_event is not None:
                    if stop_event.wait(delay):
                        results["stopped"] = True
                        break
                else:
                    time.sleep(delay)

            except Exception as exc:
                results["errors"] += 1
                err_str = str(exc).lower()
                if "login_required" in err_str or "loginrequired" in err_str:
                    self.risk.notify_session_expired(self.username)
                    break
                self.risk.record_error(self.username, exc)
                logger.error(
                    "[%s] Erro ao ver stories de %s: %s", self.username, uid, exc
                )
                if self.risk.is_paused(self.username):
                    break

        logger.info(
            "[%s] Stories — vistos=%s sem_story=%s erros=%s",
            self.username,
            results["viewed"],
            results["no_story"],
            results["errors"],
        )
        return results
