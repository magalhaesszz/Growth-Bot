import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock

from supabase import create_client

from config import (
    SUPABASE_KEY,
    SUPABASE_URL,
    VIDEO_SETTINGS_REMOTE,
)

logger = logging.getLogger(__name__)

DEFAULTS = {
    "video_width": 800,
    "position_x": 0.5,
    "position_y": 0.25,
    "output_crf": 18,
    "output_fps": 30,
    "antiban": True,
    "fix_mirror": False,
    "auto_crop_borders": True,
}

_RULES = {
    "video_width": (int, 100, 1080),
    "position_x": (float, 0.0, 1.0),
    "position_y": (float, 0.0, 1.0),
    "output_crf": (int, 0, 51),
    "output_fps": (int, 1, 60),
    "antiban": (bool, None, None),
    "fix_mirror": (bool, None, None),
    "auto_crop_borders": (bool, None, None),
}

_PATH = Path(
    os.getenv("VIDEO_CONFIG_PATH", "").strip()
    or os.path.join(tempfile.gettempdir(), "growth-bot-video-config.json")
)
_LOCK = Lock()


def validate_value(key: str, value):
    if key not in _RULES:
        raise ValueError(f"Configuração desconhecida: {key}")
    expected, minimum, maximum = _RULES[key]
    if expected is bool:
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key} deve ser true ou false")
    try:
        parsed = expected(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valor inválido para {key}") from exc
    if isinstance(value, float) and expected is int and not value.is_integer():
        raise ValueError(f"{key} deve ser inteiro")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{key} deve estar entre {minimum} e {maximum}")
    return parsed


def _sanitize(saved: dict | None) -> dict:
    valid = {}
    for key, value in (saved or {}).items():
        try:
            valid[key] = validate_value(key, value)
        except ValueError:
            continue
    return valid


def _read_local_all() -> dict:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_local_all(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, _PATH)


def _read_remote(user_id: int) -> dict | None:
    if not VIDEO_SETTINGS_REMOTE:
        return None
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = (
            sb.table("video_settings")
            .select("config")
            .eq("user_id", int(user_id))
            .limit(1)
            .execute()
        )
        if res.data:
            value = res.data[0].get("config")
            return value if isinstance(value, dict) else {}
        return {}
    except Exception as exc:
        logger.warning(
            "Video settings remoto indisponivel (%s); usando cache local.",
            type(exc).__name__,
        )
        return None


def _write_remote(user_id: int, config: dict) -> bool:
    if not VIDEO_SETTINGS_REMOTE:
        return False
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb.table("video_settings").upsert(
            {"user_id": int(user_id), "config": config}, on_conflict="user_id"
        ).execute()
        return True
    except Exception as exc:
        logger.warning(
            "Falha ao persistir video settings no Supabase (%s).",
            type(exc).__name__,
        )
        return False


def _delete_remote(user_id: int) -> bool:
    if not VIDEO_SETTINGS_REMOTE:
        return False
    try:
        create_client(SUPABASE_URL, SUPABASE_KEY).table("video_settings").delete().eq(
            "user_id", int(user_id)
        ).execute()
        return True
    except Exception as exc:
        logger.warning(
            "Falha ao resetar video settings remoto (%s).", type(exc).__name__
        )
        return False


def _read_saved(user_id: int) -> dict:
    remote = _read_remote(user_id)
    if remote is not None:
        return _sanitize(remote)
    with _LOCK:
        return _sanitize(_read_local_all().get(str(user_id), {}))


def _write_local_user(user_id: int, values: dict) -> None:
    with _LOCK:
        data = _read_local_all()
        data[str(user_id)] = values
        _write_local_all(data)


def get_config(user_id: int) -> dict:
    return {**DEFAULTS, **_read_saved(user_id)}


def set_values(user_id: int, values: dict) -> dict:
    validated = {key: validate_value(key, value) for key, value in values.items()}
    current = _read_saved(user_id)
    current.update(validated)
    current = _sanitize(current)

    # Sempre mantenha um cache local. Em producao o Supabase e a fonte
    # persistente; o cache evita perder uma alteracao em falha transitoria.
    _write_local_user(user_id, current)
    _write_remote(user_id, current)
    return {**DEFAULTS, **current}


def reset(user_id: int) -> dict:
    _delete_remote(user_id)
    with _LOCK:
        data = _read_local_all()
        data.pop(str(user_id), None)
        _write_local_all(data)
    return DEFAULTS.copy()
