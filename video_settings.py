import json
import os
import tempfile
from pathlib import Path
from threading import Lock


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


def _read_all() -> dict:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_all(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, _PATH)


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


def get_config(user_id: int) -> dict:
    with _LOCK:
        saved = _read_all().get(str(user_id), {})
    valid = {}
    for key, value in saved.items():
        try:
            valid[key] = validate_value(key, value)
        except ValueError:
            continue
    return {**DEFAULTS, **valid}


def set_values(user_id: int, values: dict) -> dict:
    validated = {key: validate_value(key, value) for key, value in values.items()}
    with _LOCK:
        data = _read_all()
        current = data.get(str(user_id), {})
        current.update(validated)
        data[str(user_id)] = current
        _write_all(data)
    return {**DEFAULTS, **current}


def reset(user_id: int) -> dict:
    with _LOCK:
        data = _read_all()
        data.pop(str(user_id), None)
        _write_all(data)
    return DEFAULTS.copy()
