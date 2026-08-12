import time
import random
import logging

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
    ) -> dict:
        results = {"viewed": 0, "no_story": 0, "errors": 0}

        for uid in user_ids[:max_per_run]:
            if self.risk.is_paused(self.username):
                break

            try:
                stories = self.cl.user_stories(int(uid))
                if not stories:
                    results["no_story"] += 1
                    continue

                story_ids = [s.pk for s in stories]
                self.cl.story_seen(story_ids)
                results["viewed"] += 1
                self.risk.record_success(self.username)
                logger.debug(f"[{self.username}] Stories vistos: user {uid}")
                time.sleep(random.uniform(delay_min, delay_max))

            except Exception as e:
                results["errors"] += 1
                self.risk.record_error(self.username, e)
                logger.error(f"[{self.username}] Erro ao ver stories de {uid}: {e}")

                if self.risk.is_paused(self.username):
                    break

        logger.info(
            f"[{self.username}] Stories — vistos: {results['viewed']}, sem story: {results['no_story']}"
        )
        return results
