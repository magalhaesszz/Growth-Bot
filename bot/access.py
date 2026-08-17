"""
Módulo central de controle de acesso.
Usado por todos os handlers para verificar permissões.
"""
from config import TELEGRAM_OWNER_ID

# Cache em memória dos usuários autorizados
_ALLOWED_USERS: dict[int, dict] = {}


def load_users() -> dict[int, dict]:
    """Carrega usuários do Supabase para memória."""
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
                "is_admin": r.get("is_admin", False),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Erro ao carregar usuários: {e}")
    return _ALLOWED_USERS


def has_access(user_id: int) -> bool:
    """Retorna True se o usuário tem acesso ao bot."""
    if user_id == TELEGRAM_OWNER_ID:
        return True
    load_users()
    return user_id in _ALLOWED_USERS


def is_owner(user_id: int) -> bool:
    """Retorna True apenas para o dono do bot."""
    return user_id == TELEGRAM_OWNER_ID


def is_admin(user_id: int) -> bool:
    """Retorna True para o dono ou admins cadastrados."""
    if user_id == TELEGRAM_OWNER_ID:
        return True
    load_users()
    info = _ALLOWED_USERS.get(user_id, {})
    return info.get("is_admin", False)


def get_all_users() -> dict[int, dict]:
    """Retorna todos os usuários autorizados (sem o owner)."""
    load_users()
    return dict(_ALLOWED_USERS)


def save_user(user_id: int, data: dict):
    """Salva usuário no Supabase."""
    try:
        from database.operations import DB
        DB().sb.table("bot_users").upsert({"user_id": int(user_id), **data}).execute()
        _ALLOWED_USERS[int(user_id)] = data
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Erro ao salvar usuário: {e}")


def remove_user(user_id: int):
    """Remove usuário do Supabase."""
    try:
        from database.operations import DB
        DB().sb.table("bot_users").delete().eq("user_id", int(user_id)).execute()
        _ALLOWED_USERS.pop(int(user_id), None)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Erro ao remover usuário: {e}")
