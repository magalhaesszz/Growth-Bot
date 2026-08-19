import logging

logger = logging.getLogger(__name__)


class ProfileScorer:
    """Calcula score 0-100 sem transformar dados ausentes em sinais positivos."""

    def __init__(
        self,
        min_posts: int = 3,
        max_following_ratio: float = 10.0,
        require_photo: bool = True,
    ):
        self.min_posts = min_posts
        self.max_following_ratio = max_following_ratio
        self.require_photo = require_photo

    def score(self, profile: dict) -> int:
        points = 0

        if profile.get("profile_pic_url"):
            points += 20

        media_count = profile.get("media_count")
        if isinstance(media_count, (int, float)):
            if media_count >= self.min_posts:
                points += 20
            elif media_count > 0:
                points += 10

        followers = profile.get("follower_count")
        following = profile.get("following_count")
        if isinstance(followers, (int, float)) and isinstance(
            following, (int, float)
        ):
            ratio = following / max(followers, 1)
            if ratio <= 2.0:
                points += 30
            elif ratio <= 5.0:
                points += 20
            elif ratio <= self.max_following_ratio:
                points += 10

        is_private = profile.get("is_private")
        if is_private is False:
            points += 15

        if str(profile.get("full_name", "") or "").strip():
            points += 15

        return min(points, 100)

    def passes(self, profile: dict, min_score: int) -> bool:
        score = self.score(profile)
        username = profile.get("username", "?")
        logger.debug("Score @%s: %s (minimo=%s)", username, score, min_score)
        return score >= min_score


class BlacklistFilter:
    def __init__(self, terms: list[str]):
        self.keywords = [term.lower() for term in terms if term]

    def is_blocked(self, profile: dict) -> bool:
        username = str(profile.get("username", "") or "").lower()
        full_name = str(profile.get("full_name", "") or "").lower()
        for keyword in self.keywords:
            if keyword in username or keyword in full_name:
                logger.debug(
                    "@%s bloqueado pela blacklist (termo=%s)", username, keyword
                )
                return True
        return False


class WhitelistFilter:
    def __init__(self, usernames: list[str]):
        self.usernames = {u.lower().lstrip("@") for u in usernames}

    def is_protected(self, username: str) -> bool:
        return username.lower().lstrip("@") in self.usernames
