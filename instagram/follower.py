import time
import random
import logging
from datetime import datetime, date

from instagram.score import ProfileScorer, BlacklistFilter
from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)


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
        self._followed_today = 0
        self._today = date.today()

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._today:
            self._followed_today = 0
            self._today = today

    def _can_follow(self, daily_limit: int) -> bool:
        self._reset_if_new_day()
        if self.risk.is_paused(self.username):
            logger.warning(f"[{self.username}] Conta pausada — follow bloqueado.")
            return False
        if self._followed_today >= daily_limit:
            logger.info(f"[{self.username}] Limite diário atingido ({daily_limit}).")
            return False
        return True

    def follow_batch(
        self,
        profiles: list[dict],
        daily_limit: int,
        min_score: int,
        delay_min: int,
        delay_max: int,
        on_success=None,  # callback(username, user_id) após cada follow
    ) -> dict:
        """
        Segue perfis de uma lista respeitando limites, score e blacklist.
        Retorna resumo {"followed": N, "skipped": N, "errors": N}.
        """
        results = {"followed": 0, "skipped": 0, "errors": 0}

        for profile in profiles:
            if not self._can_follow(daily_limit):
                break

            uname = profile.get("username", "")
            uid = profile.get("user_id", "")

            # blacklist
            if self.blacklist.is_blocked(profile):
                results["skipped"] += 1
                continue

            # score mínimo
            if not self.scorer.passes(profile, min_score):
                results["skipped"] += 1
                continue

            # tenta seguir
            try:
                self.cl.user_follow(int(uid))
                self._followed_today += 1
                results["followed"] += 1
                self.risk.record_success(self.username)
                self.client.save_session()
                logger.info(f"[{self.username}] Seguiu @{uname} ✓")

                if on_success:
                    on_success(uname, uid)

                # delay humano
                delay = random.uniform(delay_min, delay_max)
                time.sleep(delay)

            except Exception as e:
                results["errors"] += 1
                err_str = str(e).lower()

                # Sessao expirada / logout forcado pelo Instagram
                if any(k in err_str for k in ("login_required", "loginrequired", "sessionid")):
                    self.risk.notify_session_expired(self.username)
                    results["errors"] += 1
                    break  # para o batch — sessao invalida, sem sentido continuar

                self.risk.record_error(self.username, e)
                logger.error(f"[{self.username}] Erro ao seguir @{uname}: {e}")

                if self.risk.is_paused(self.username):
                    break  # para o batch se conta foi pausada

                time.sleep(random.uniform(10, 30))  # cooldown após erro

        logger.info(
            f"[{self.username}] Batch concluído — "
            f"seguidos: {results['followed']}, "
            f"pulados: {results['skipped']}, "
            f"erros: {results['errors']}"
        )
        return results
