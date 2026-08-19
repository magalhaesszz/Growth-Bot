import json
import logging
from typing import Any

from supabase import create_client

from config import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)


class BotStateDB:
    """Pequeno armazenamento chave/valor persistente no Supabase.

    A tabela ``bot_state`` guarda estados operacionais que precisam sobreviver a
    restart, como modo manual, pausa de risco e modo seguro. Falhas de acesso ao
    estado nunca devem derrubar o bot; os chamadores podem usar um fallback em
    memoria quando necessario.
    """

    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    def get(self, key: str, default: str | None = None) -> str | None:
        try:
            res = (
                self.sb.table("bot_state")
                .select("value")
                .eq("key", key)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0].get("value", default)
        except Exception as exc:
            logger.warning("Falha ao ler bot_state[%s]: %s", key, type(exc).__name__)
        return default

    def set(self, key: str, value: str) -> bool:
        try:
            self.sb.table("bot_state").upsert(
                {"key": key, "value": str(value)}, on_conflict="key"
            ).execute()
            return True
        except Exception as exc:
            logger.warning("Falha ao salvar bot_state[%s]: %s", key, type(exc).__name__)
            return False

    def delete(self, key: str) -> bool:
        try:
            self.sb.table("bot_state").delete().eq("key", key).execute()
            return True
        except Exception as exc:
            logger.warning("Falha ao remover bot_state[%s]: %s", key, type(exc).__name__)
            return False

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("bot_state[%s] contem JSON invalido.", key)
            return default

    def set_json(self, key: str, value: Any) -> bool:
        return self.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
