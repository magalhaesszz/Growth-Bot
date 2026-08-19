import json
import logging

import httpx

from config import (
    VIDEO_API_SECRET,
    VIDEO_API_URL,
    VIDEO_MAX_BATCH_MB,
    VIDEO_MAX_FILE_MB,
)

logger = logging.getLogger(__name__)

HEADERS = {"x-api-secret": VIDEO_API_SECRET}
TIMEOUT = httpx.Timeout(300.0, connect=15.0)
_MB = 1024 * 1024


def _check_configured() -> None:
    if not VIDEO_API_URL or not VIDEO_API_SECRET:
        raise ValueError("VIDEO_API_URL e VIDEO_API_SECRET não configurados no servidor.")


def _size_mb(data: bytes) -> float:
    return len(data) / _MB


def _validate_video_size(data: bytes, label: str = "video") -> None:
    size = _size_mb(data)
    if size > VIDEO_MAX_FILE_MB:
        raise ValueError(
            f"{label} tem {size:.1f} MB; limite seguro deste bot: {VIDEO_MAX_FILE_MB} MB."
        )


def _validate_batch_size(videos: list[tuple[bytes, str]]) -> None:
    total = 0.0
    for data, name in videos:
        _validate_video_size(data, name)
        total += _size_mb(data)
    if total > VIDEO_MAX_BATCH_MB:
        raise ValueError(
            f"Lote tem {total:.1f} MB; limite seguro: {VIDEO_MAX_BATCH_MB} MB."
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
        _validate_video_size(video_bytes, filename)
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/processar",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={"account_id": account_id, "config_json": json.dumps(cfg or {})},
            )
        if not response.is_success:
            return {"ok": False, "error": _response_error(response)}
        _validate_video_size(response.content, "video processado")
        disposition = response.headers.get("content-disposition", "")
        output_name = filename
        if "filename=" in disposition:
            output_name = disposition.split("filename=")[-1].strip('"')
        return {
            "ok": True,
            "video_bytes": response.content,
            "filename": output_name,
            "size_mb": round(_size_mb(response.content), 2),
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
        _validate_video_size(video_bytes, filename)
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
        _validate_video_size(video_bytes, filename)
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
        _validate_batch_size(videos)
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


def _download_result(path: str) -> bytes | None:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(path, headers=HEADERS)
        if not response.is_success:
            return None
        _validate_video_size(response.content, "video retornado")
        return response.content
    except Exception as exc:
        logger.warning("Download da Video API falhou: %s", type(exc).__name__)
        return None


def download_lote_video(job_id: str) -> bytes | None:
    return _download_result(f"{VIDEO_API_URL}/api/v1/download/{job_id}")


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
            response = client.delete(f"{VIDEO_API_URL}/api/v1/limpar", headers=HEADERS)
        if response.is_success:
            return response.json()
        return {"ok": False, "error": _response_error(response)}
    except Exception as exc:
        return _error(exc)


def download_link(url: str) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/download",
                headers=HEADERS,
                json={"url": url},
            )
        if not response.is_success:
            return {"ok": False, "error": _response_error(response)}
        _validate_video_size(response.content, "video baixado")
        filename = response.headers.get("X-Filename", "video.mp4")
        return {
            "ok": True,
            "video_bytes": response.content,
            "filename": filename,
            "size_mb": round(_size_mb(response.content), 2),
        }
    except Exception as exc:
        return _error(exc)


def buscar_video_baixado(job_id: str) -> bytes | None:
    return _download_result(f"{VIDEO_API_URL}/api/v1/download/{job_id}")


def editar_video(
    video_bytes: bytes,
    filename: str,
    watermark_text: str = "",
    caption_text: str = "",
    crop_start: float = 0.0,
    crop_end: float = 0.0,
    speed: float = 0.0,
    flip: bool = False,
) -> dict:
    try:
        _check_configured()
        _validate_video_size(video_bytes, filename)
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{VIDEO_API_URL}/api/v1/editar",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={
                    "watermark_text": watermark_text,
                    "caption_text": caption_text,
                    "crop_start": str(crop_start),
                    "crop_end": str(crop_end),
                    "speed": str(speed),
                    "flip": str(flip).lower(),
                },
            )
        if not response.is_success:
            return {"ok": False, "error": _response_error(response)}
        _validate_video_size(response.content, "video editado")
        return {
            "ok": True,
            "video_bytes": response.content,
            "filename": f"editado_{filename}",
            "size_mb": round(_size_mb(response.content), 2),
        }
    except Exception as exc:
        return _error(exc)
