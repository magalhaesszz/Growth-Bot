import json
import logging

import httpx

from config import VIDEO_API_SECRET, VIDEO_API_URL

logger = logging.getLogger(__name__)

HEADERS = {"x-api-secret": VIDEO_API_SECRET}
TIMEOUT = httpx.Timeout(300.0, connect=15.0)


def _check_configured() -> None:
    if not VIDEO_API_URL or not VIDEO_API_SECRET:
        raise ValueError(
            "VIDEO_API_URL e VIDEO_API_SECRET não configurados no servidor."
        )


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("detail") or payload.get("error") or payload)
    except (ValueError, json.JSONDecodeError):
        pass
    return response.text.strip() or f"HTTP {response.status_code}"


def _error(exc: Exception) -> dict:
    if isinstance(exc, httpx.TimeoutException):
        message = "Timeout: o servidor de vídeo demorou demais para responder."
    else:
        message = str(exc)
    logger.warning("Falha na Video API: %s", type(exc).__name__)
    return {"ok": False, "error": message}


def salvar_fundo(fundo_bytes: bytes, filename: str, account_id: str = "default") -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/fundo",
                headers=HEADERS,
                files={"fundo": (filename, fundo_bytes, "application/octet-stream")},
                data={"account_id": account_id},
            )
        if response.is_success:
            return {"ok": True, "message": response.json().get("message", "Fundo salvo.")}
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def ver_fundo(account_id: str = "default") -> bytes | None:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(
                f"{VIDEO_API_URL}/api/v1/fundo",
                headers=HEADERS,
                params={"account_id": account_id},
            )
        return response.content if response.is_success else None
    except Exception:
        return None


def processar_video(
    video_bytes: bytes,
    filename: str,
    account_id: str = "default",
    cfg: dict | None = None,
) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/processar",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={"account_id": account_id, "config_json": json.dumps(cfg or {})},
            )
        if not response.is_success:
            return {"ok": False, "error": _response_error(response)}
        disposition = response.headers.get("content-disposition", "")
        output_name = filename
        if "filename=" in disposition:
            output_name = disposition.split("filename=")[-1].strip('"')
        return {
            "ok": True,
            "video_bytes": response.content,
            "filename": output_name,
            "size_mb": round(len(response.content) / (1024 * 1024), 2),
        }
    except Exception as exc:
        return _error(exc)


def gerar_preview(
    video_bytes: bytes,
    filename: str,
    account_id: str = "default",
    cfg: dict | None = None,
) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/preview",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={"account_id": account_id, "config_json": json.dumps(cfg or {})},
            )
        if response.is_success:
            return {"ok": True, "image_bytes": response.content}
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def criar_editor_session(
    video_bytes: bytes,
    filename: str,
    account_id: str = "default",
    cfg: dict | None = None,
) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/editor/session",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={"account_id": account_id, "config_json": json.dumps(cfg or {})},
            )
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def obter_editor_result(token: str) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
            response = client.get(
                f"{VIDEO_API_URL}/api/v1/editor/{token}/result",
                headers=HEADERS,
            )
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def processar_lote(
    videos: list[tuple[bytes, str]],
    account_id: str = "default",
    cfg: dict | None = None,
) -> dict:
    try:
        _check_configured()
        files = [("videos", (name, data, "video/mp4")) for data, name in videos]
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/processar/lote",
                headers=HEADERS,
                files=files,
                data={"account_id": account_id, "config_json": json.dumps(cfg or {})},
            )
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def download_lote_video(job_id: str) -> bytes | None:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(
                f"{VIDEO_API_URL}/api/v1/download/{job_id}", headers=HEADERS
            )
        return response.content if response.is_success else None
    except Exception:
        return None


def api_status() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            response = client.get(f"{VIDEO_API_URL}/api/v1/status", headers=HEADERS)
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def config_default() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            response = client.get(
                f"{VIDEO_API_URL}/api/v1/config/default", headers=HEADERS
            )
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def limpar_tmp() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            response = client.delete(
                f"{VIDEO_API_URL}/api/v1/limpar", headers=HEADERS
            )
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)
