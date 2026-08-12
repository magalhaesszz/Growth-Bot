import logging

logger = logging.getLogger(__name__)


class ProfileScorer:
    """
    Calcula score 0-100 de um perfil antes de seguir.
    Critérios: tem foto, tem posts, ratio seguidores/seguindo,
    conta não privada (ou privada aceitável), bio preenchida.
    """

    def __init__(
        self,
        min_posts: int = 3,
        max_following_ratio: float = 10.0,  # seguindo/seguidores máximo
        require_photo: bool = True,
    ):
        self.min_posts = min_posts
        self.max_following_ratio = max_following_ratio
        self.require_photo = require_photo

    def score(self, profile: dict) -> int:
        """Retorna score de 0 a 100."""
        points = 0

        # Tem foto de perfil (20 pts)
        if profile.get("profile_pic_url"):
            points += 20

        # Posts mínimos (20 pts)
        media_count = profile.get("media_count", 0)
        if media_count >= self.min_posts:
            points += 20
        elif media_count > 0:
            points += 10

        # Ratio seguidores/seguindo (30 pts)
        followers = profile.get("follower_count", 0)
        following = profile.get("following_count", 1)
        ratio = following / max(followers, 1)
        if ratio <= 2.0:
            points += 30
        elif ratio <= 5.0:
            points += 20
        elif ratio <= self.max_following_ratio:
            points += 10

        # Conta não privada (15 pts)
        if not profile.get("is_private", True):
            points += 15

        # Tem nome completo (15 pts — sinal de conta real)
        if profile.get("full_name", "").strip():
            points += 15

        return min(points, 100)

    def passes(self, profile: dict, min_score: int) -> bool:
        s = self.score(profile)
        username = profile.get("username", "?")
        logger.debug(f"Score @{username}: {s} (mínimo: {min_score})")
        return s >= min_score


class BlacklistFilter:
    """Verifica se um perfil deve ser ignorado pela blacklist."""

    def __init__(self, terms: list[str]):
        # termos em lower case para comparação
        self.keywords = [t.lower() for t in terms if t]

    def is_blocked(self, profile: dict) -> bool:
        username = profile.get("username", "").lower()
        full_name = profile.get("full_name", "").lower()

        for kw in self.keywords:
            if kw in username or kw in full_name:
                logger.debug(f"@{username} bloqueado pela blacklist (termo: '{kw}')")
                return True
        return False


class WhitelistFilter:
    """Verifica se um perfil está na whitelist (nunca deixar de seguir)."""

    def __init__(self, usernames: list[str]):
        self.usernames = {u.lower().lstrip("@") for u in usernames}

    def is_protected(self, username: str) -> bool:
        return username.lower() in self.usernames
