import logging
import random
import time

from database.state import BotStateDB
from instagram.risk_detector import RiskDetector

logger = logging.getLogger(__name__)

_SEEN_STATE_PREFIX = "stories_seen:"
_SEEN_TTL_SECONDS = 48 * 60 * 60


class StoriesViewer:
    """Busca stories, marca ``seen`` e evita repetir o mesmo story.

    O Instagram identifica cada story por um PK. Guardamos os PKs vistos por
    cerca de 48h em ``bot_state``; como um story normal expira antes disso, um
    novo PK sempre volta a ser elegivel imediatamente.
    """

    def __init__(self, ig_client, risk_detector: RiskDetector, state_db=None):
        self.cl = ig_client.api
        self.client = ig_client
        self.username = ig_client.username
        self.risk = risk_detector
        self.state_db = state_db if state_db is not None else BotStateDB()

    @property
    def _seen_state_key(self) -> str:
        return f"{_SEEN_STATE_PREFIX}{self.username.casefold()}"

    @staticmethod
    def _story_pk(story) -> str | None:
        pk = getattr(story, "pk", None)
        if pk is None:
            return None
        value = str(pk).strip()
        return value or None

    @staticmethod
    def _normalize_story_pk(pk: str):
        """Mantem compatibilidade com story_seen, que aceita PK numerico."""
        text = str(pk)
        return int(text) if text.isdigit() else text

    @staticmethod
    def _extract_tray_user_id(entry: dict) -> str | None:
        if not isinstance(entry, dict):
            return None

        nested_reel = entry.get("reel") if isinstance(entry.get("reel"), dict) else {}
        for user in (
            entry.get("user"),
            entry.get("owner"),
            nested_reel.get("user"),
            nested_reel.get("owner"),
        ):
            if not isinstance(user, dict):
                continue
            uid = user.get("pk") or user.get("id")
            if uid is not None and str(uid).isdigit():
                return str(uid)

        for source in (entry, nested_reel):
            for key in ("id", "reel_id"):
                uid = source.get(key)
                if uid is not None and str(uid).isdigit():
                    return str(uid)
        return None

    @staticmethod
    def _extract_tray_marker(entry: dict) -> str | None:
        """Retorna um marcador que muda quando o tray recebe story mais novo."""
        if not isinstance(entry, dict):
            return None
        nested_reel = entry.get("reel") if isinstance(entry.get("reel"), dict) else {}
        for source in (entry, nested_reel):
            for key in (
                "latest_reel_media",
                "latest_besties_reel_media",
                "latest_reel_media_seen",
            ):
                value = source.get(key)
                if value not in (None, "", 0, "0"):
                    return str(value)

            items = source.get("items")
            if isinstance(items, list) and items:
                item_ids = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    pk = item.get("pk") or item.get("id")
                    if pk is not None:
                        item_ids.append(str(pk))
                if item_ids:
                    return ",".join(item_ids)
        return None

    def _load_seen_state(self) -> dict[str, float]:
        now = time.time()
        raw = self.state_db.get_json(self._seen_state_key, {}) or {}
        if not isinstance(raw, dict):
            return {}

        clean: dict[str, float] = {}
        for pk, seen_at in raw.items():
            try:
                timestamp = float(seen_at)
            except (TypeError, ValueError):
                continue
            if now - timestamp <= _SEEN_TTL_SECONDS:
                clean[str(pk)] = timestamp
        return clean

    def _save_seen_state(self, state: dict[str, float]) -> None:
        cutoff = time.time() - _SEEN_TTL_SECONDS
        compact = {
            str(pk): float(seen_at)
            for pk, seen_at in state.items()
            if float(seen_at) >= cutoff
        }
        self.state_db.set_json(self._seen_state_key, compact)

    def get_following_user_ids(self) -> list[str]:
        """Retorna a lista real de contas que o perfil segue no Instagram."""
        try:
            raw = self.cl.user_following(self.cl.user_id, amount=0)
        except TypeError:
            # Compatibilidade com releases que nao aceitam ``amount`` nomeado.
            raw = self.cl.user_following(self.cl.user_id)

        ids: list[str] = []
        if isinstance(raw, dict):
            candidates = raw.keys()
        else:
            candidates = (
                getattr(user, "pk", None) or getattr(user, "id", None)
                for user in (raw or [])
            )

        seen: set[str] = set()
        for uid in candidates:
            if uid is None:
                continue
            value = str(uid)
            if not value or value in seen:
                continue
            seen.add(value)
            ids.append(value)
        return ids

    def get_tray_user_markers(
        self, allowed_user_ids: set[str] | None = None
    ) -> dict[str, str | None]:
        """Lê o tray privado e retorna apenas perfis com stories ativos.

        O tray e usado como caminho rapido. Uma varredura em rodizio continua
        existindo no scheduler para cobrir qualquer perfil omitido pelo tray.
        """
        try:
            payload = self.cl.get_reels_tray_feed(reason="pull_to_refresh")
        except TypeError:
            payload = self.cl.get_reels_tray_feed()

        tray = (payload or {}).get("tray") or []
        if isinstance(tray, dict):
            tray = list(tray.values())

        allowed = {str(uid) for uid in allowed_user_ids} if allowed_user_ids else None
        markers: dict[str, str | None] = {}
        for entry in tray:
            uid = self._extract_tray_user_id(entry)
            if not uid:
                continue
            if allowed is not None and uid not in allowed:
                continue
            if str(getattr(self.cl, "user_id", "")) == uid:
                continue
            markers[uid] = self._extract_tray_marker(entry)
        return markers

    @staticmethod
    def _is_session_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "login_required" in text or "loginrequired" in text

    def view_stories_for_users(
        self,
        user_ids: list[str],
        max_per_run: int = 30,
        delay_min: float = 0.5,
        delay_max: float = 1.2,
        stop_event=None,
    ) -> dict:
        """Marca como vistos somente os stories ainda nao processados.

        ``story_seen`` e chamado com os PKs novos. O PK so e persistido depois
        que a chamada de seen retorna sucesso, entao uma falha nao vira falso
        positivo de visualizacao.
        """
        results = {
            "viewed": 0,
            "already_seen": 0,
            "no_story": 0,
            "errors": 0,
            "stopped": False,
            "new_story_pks": [],
            "story_user_ids": [],
            "failed_user_ids": [],
        }
        seen_state = self._load_seen_state()
        seen_pks = set(seen_state)
        state_changed = False
        targets = [str(uid) for uid in user_ids[: max(0, int(max_per_run))]]

        for index, uid in enumerate(targets):
            if (stop_event is not None and stop_event.is_set()) or self.risk.is_paused(
                self.username
            ):
                results["stopped"] = bool(stop_event and stop_event.is_set())
                break

            stop_now = False
            try:
                stories = self.cl.user_stories(int(uid))
                if not stories:
                    results["no_story"] += 1
                else:
                    results["story_user_ids"].append(uid)
                    story_pks = [
                        pk for pk in (self._story_pk(story) for story in stories) if pk
                    ]
                    unseen = [pk for pk in story_pks if pk not in seen_pks]
                    results["already_seen"] += len(story_pks) - len(unseen)

                    if unseen:
                        marked = self.cl.story_seen(
                            [self._normalize_story_pk(pk) for pk in unseen]
                        )
                        if marked is False:
                            raise RuntimeError("story_seen retornou False")

                        seen_at = time.time()
                        for pk in unseen:
                            seen_pks.add(pk)
                            seen_state[pk] = seen_at
                        state_changed = True
                        results["viewed"] += len(unseen)
                        results["new_story_pks"].extend(unseen)
                        self.risk.record_success(self.username)
                        logger.info(
                            "[%s] Stories marcados como vistos: user=%s novos=%s",
                            self.username,
                            uid,
                            len(unseen),
                        )

            except Exception as exc:
                results["errors"] += 1
                results["failed_user_ids"].append(uid)
                if self._is_session_error(exc):
                    self.risk.notify_session_expired(self.username)
                    stop_now = True
                else:
                    self.risk.record_error(self.username, exc)
                logger.warning(
                    "[%s] Erro ao monitorar stories de %s: %s",
                    self.username,
                    uid,
                    type(exc).__name__,
                )
                if self.risk.is_paused(self.username):
                    stop_now = True

            if stop_now:
                break

            if index < len(targets) - 1 and delay_max > 0:
                low = max(0.0, float(delay_min))
                high = max(low, float(delay_max))
                delay = random.uniform(low, high)
                if stop_event is not None:
                    if stop_event.wait(delay):
                        results["stopped"] = True
                        break
                else:
                    time.sleep(delay)

        if state_changed:
            self._save_seen_state(seen_state)

        logger.info(
            "[%s] Stories — novos=%s ja_vistos=%s sem_story=%s erros=%s",
            self.username,
            results["viewed"],
            results["already_seen"],
            results["no_story"],
            results["errors"],
        )
        return results
