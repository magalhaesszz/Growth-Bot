import logging
import re

from instagrapi.exceptions import PrivateError, UserNotFound

logger = logging.getLogger(__name__)


def _extract_username(url_or_handle: str) -> str:
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
        username = _extract_username(url_or_handle)
        try:
            info = self.cl.user_info_by_username(username)
            return {"user_id": str(info.pk), "username": info.username}
        except UserNotFound:
            logger.error("Pagina nao encontrada: %s", username)
            return None
        except Exception as exc:
            logger.error("Erro ao resolver pagina %s: %s", username, exc)
            return None

    def get_followers(
        self,
        page_user_id: str,
        page_username: str,
        limit: int = 200,
        already_following: set | None = None,
        stop_event=None,
    ) -> list[dict]:
        """Extrai seguidores sem inventar metricas ausentes de ``UserShort``."""
        already_following = already_following or set()
        followers: list[dict] = []

        try:
            logger.info(
                "[%s] Raspando seguidores de @%s (limite=%s)",
                self.username,
                page_username,
                limit,
            )
            users = []
            try:
                users = self.cl.user_followers_v1(page_user_id, amount=limit)
            except Exception as first_exc:
                logger.warning(
                    "[%s] user_followers_v1 falhou (%s); tentando user_followers",
                    self.username,
                    type(first_exc).__name__,
                )
                try:
                    raw = self.cl.user_followers(page_user_id, amount=limit)
                    users = list(raw.values())
                except Exception as second_exc:
                    logger.error(
                        "[%s] user_followers tambem falhou: %s",
                        self.username,
                        type(second_exc).__name__,
                    )

            for user in users:
                if stop_event is not None and stop_event.is_set():
                    break
                uid = str(user.pk)
                if uid in already_following:
                    continue
                followers.append(
                    {
                        "user_id": uid,
                        "username": user.username,
                        "full_name": getattr(user, "full_name", "") or "",
                        "is_private": getattr(user, "is_private", None),
                        "follower_count": getattr(user, "follower_count", None),
                        "following_count": getattr(user, "following_count", None),
                        "media_count": getattr(user, "media_count", None),
                        "profile_pic_url": (
                            str(user.profile_pic_url)
                            if getattr(user, "profile_pic_url", None)
                            else None
                        ),
                    }
                )

            logger.info(
                "[%s] %s perfis raspados de @%s",
                self.username,
                len(followers),
                page_username,
            )
        except PrivateError:
            logger.warning("@%s e privada; seguidores indisponiveis.", page_username)
        except Exception as exc:
            logger.error("Erro ao raspar @%s: %s", page_username, exc)

        return followers
