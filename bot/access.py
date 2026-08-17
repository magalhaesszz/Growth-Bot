"""
Módulo central de controle de acesso.
"""
import logging
from config import TELEGRAM_OWNER_ID

logger = logging.getLogger(__name__)

# Cache em memória
_ALLOWED_USERS: dict[int, dict] = {}


def load_users() -> dict[int, dict]:
    """Carrega usuários do Supabase."""
    try:
        from database.operations import DB
        rows = DB().sb.table("bot_users").select("*").execute().data or []
        _ALLOWED_USERS.clear()
        for r in rows:
            uid = int(r["user_id"])
            _ALLOWED_USERS[uid] = {
                "username": r.get("username", "?"),
                "name":     r.get("name", "?"),
                "added_at": r.get("added_at", "?"),
                "is_admin": bool(r.get("is_admin", False)),
            }
        logger.info(f"Usuários carregados: {len(_ALLOWED_USERS)}")
    except Exception as e:
        logger.warning(f"Erro ao carregar usuários: {e}")
    return _ALLOWED_USERS


def has_access(user_id: int) -> bool:
    if user_id == TELEGRAM_OWNER_ID:
        return True
    load_users()
    return user_id in _ALLOWED_USERS


def is_owner(user_id: int) -> bool:
    return user_id == TELEGRAM_OWNER_ID


def is_admin(user_id: int) -> bool:
    if user_id == TELEGRAM_OWNER_ID:
        return True
    load_users()
    return _ALLOWED_USERS.get(user_id, {}).get("is_admin", False)


def get_all_users() -> dict[int, dict]:
    load_users()
    return dict(_ALLOWED_USERS)


def save_user(user_id: int, data: dict):
    """Salva/atualiza usuário no Supabase — só colunas que existem na tabela."""
    try:
        from database.operations import DB
        uid = int(user_id)
        payload = {
            "user_id":  uid,
            "username": data.get("username", "?"),
            "name":     data.get("name", "?"),
            "added_at": data.get("added_at", "?"),
            "is_admin": bool(data.get("is_admin", False)),
        }
        DB().sb.table("bot_users").upsert(payload).execute()
        _ALLOWED_USERS[uid] = {k: v for k, v in payload.items() if k != "user_id"}
        logger.info(f"Usuário {uid} salvo (is_admin={payload['is_admin']})")
    except Exception as e:
        logger.warning(f"Erro ao salvar usuário {user_id}: {e}")


def remove_user(user_id: int):
    """Remove usuário do Supabase."""
    try:
        from database.operations import DB
        uid = int(user_id)
        DB().sb.table("bot_users").delete().eq("user_id", uid).execute()
        _ALLOWED_USERS.pop(uid, None)
        logger.info(f"Usuário {uid} removido")
    except Exception as e:
        logger.warning(f"Erro ao remover usuário {user_id}: {e}")
