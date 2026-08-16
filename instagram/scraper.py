import re
import logging
import time
import random
from instagrapi.exceptions import UserNotFound, PrivateError

logger = logging.getLogger(__name__)


def _extract_username(url_or_handle: str) -> str:
    """Extrai username de URL ou @handle."""
    url_or_handle = url_or_handle.strip().rstrip("/")
    match = re.search(r"instagram\.com/([^/?]+)", url_or_handle)
    if match:
        return match.group(1)
    return url_or_handle.lstrip("@")


class Scraper:
    def __init__(self, ig_client):
        self.cl = ig_client.api
        self.username = ig_client.username

    def resolve_page(self, url_or_handle: str) -> dict | None:
        """Resolve URL/handle para user_id + username da página."""
        username = _extract_username(url_or_handle)
        try:
            info = self.cl.user_info_by_username(username)
            return {"user_id": str(info.pk), "username": info.username}
        except UserNotFound:
            logger.error(f"Página não encontrada: {username}")
            return None
        except Exception as e:
            logger.error(f"Erro ao resolver página {username}: {e}")
            return None

    def get_followers(
        self,
        page_user_id: str,
        page_username: str,
        limit: int = 200,
        already_following: set = None,
    ) -> list[dict]:
        """
        Extrai seguidores de uma página-alvo.
        Retorna lista de dicts com user_id e username.
        """
        already_following = already_following or set()
        followers = []

        try:
            logger.info(f"[{self.username}] Raspando seguidores de @{page_username} (limite: {limit})")

            # Tentar user_followers_v1 (Mobile API) — funciona com sessão web
            users = []
            try:
                users = self.cl.user_followers_v1(page_user_id, amount=limit)
            except Exception as e1:
                logger.warning(f"[{self.username}] user_followers_v1 falhou: {e1} — tentando user_followers")
                try:
                    raw = self.cl.user_followers(page_user_id, amount=limit)
                    users = list(raw.values())
                except Exception as e2:
                    logger.error(f"[{self.username}] user_followers também falhou: {e2}")

            for user in users:
                uid = str(user.pk)
                if uid in already_following:
                    continue
                followers.append({
                    "user_id": uid,
                    "username": user.username,
                    "full_name": user.full_name,
                    "is_private": user.is_private,
                    "follower_count": user.follower_count,
                    "following_count": user.following_count,
                    "media_count": user.media_count,
                    "profile_pic_url": str(user.profile_pic_url) if user.profile_pic_url else None,
                })
                time.sleep(random.uniform(0.5, 1.5))

            logger.info(f"[{self.username}] {len(followers)} perfis raspados de @{page_username}")
        except PrivateError:
            logger.warning(f"@{page_username} é privada — impossível raspar seguidores.")
        except Exception as e:
            logger.error(f"Erro ao raspar @{page_username}: {e}")

        return followers
