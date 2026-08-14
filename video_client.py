import json
import logging
import os
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

VIDEO_API_URL    = os.getenv("VIDEO_API_URL", "").rstrip("/")
VIDEO_API_SECRET = os.getenv("VIDEO_API_SECRET", "")

HEADERS = {"x-api-secret": VIDEO_API_SECRET}
TIMEOUT = httpx.Timeout(300.0, connect=10.0)


def _check_configured():
    if not VIDEO_API_URL or not VIDEO_API_SECRET:
        raise ValueError(
            "VIDEO_API_URL e VIDEO_API_SECRET não configurados no .env. "
            "Adicione a URL do Railway e o segredo compartilhado."
        )


def _response_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            return str(payload.get("detail") or payload.get("error") or resp.text)
    except (ValueError, TypeError):
        pass
    return resp.text or f"HTTP {resp.status_code}"


# ─── Fundo ───────────────────────────────────────────────────

def salvar_fundo(fundo_bytes: bytes, filename: str, account_id: str = "default") -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                f"{VIDEO_API_URL}/api/v1/fundo",
                headers=HEADERS,
                files={"fundo": (filename, fundo_bytes, "image/png")},
                data={"account_id": account_id},
            )
        if resp.status_code == 200:
            return {"ok": True, "message": resp.json().get("message", "Fundo salvo.")}
        return {"ok": False, "error": _response_error(resp)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def ver_fundo(account_id: str = "default") -> bytes | None:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{VIDEO_API_URL}/api/v1/fundo",
                headers=HEADERS,
                params={"account_id": account_id},
            )
        if resp.status_code == 200:
            return resp.content
    except Exception:
        logger.warning("Não foi possível consultar o fundo da Video API.")
    return None


# ─── Processar vídeo único ───────────────────────────────────

def processar_video(
    video_bytes: bytes,
    filename: str,
    account_id: str = "default",
    cfg: dict = None,
) -> dict:
    """
    Envia vídeo para a API e retorna dict com:
    - ok: bool
    - video_bytes: bytes (se ok)
    - error: str (se não ok)
    - elapsed_s, size_mb (se ok)
    """
    try:
        _check_configured()
        config_json = json.dumps(cfg or {})
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                f"{VIDEO_API_URL}/api/v1/processar",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={"account_id": account_id, "config_json": config_json},
            )
        if resp.status_code == 200:
            cd = resp.headers.get("content-disposition", "")
            fname = filename
            if "filename=" in cd:
                fname = cd.split("filename=")[-1].strip('"')
            return {
                "ok": True,
                "video_bytes": resp.content,
                "filename": fname,
                "size_mb": round(len(resp.content) / (1024 * 1024), 2),
            }
        return {"ok": False, "error": _response_error(resp)}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Timeout: o servidor de vídeo demorou demais para responder."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def criar_editor_session(
    video_bytes: bytes,
    filename: str,
    account_id: str = "default",
    cfg: dict = None,
) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                f"{VIDEO_API_URL}/api/v1/editor/session",
                headers=HEADERS,
                files={"video": (filename, video_bytes, "video/mp4")},
                data={
                    "account_id": account_id,
                    "config_json": json.dumps(cfg or {}),
                },
            )
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "error": _response_error(resp)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def obter_editor_config(token: str) -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.get(
                f"{VIDEO_API_URL}/api/v1/editor/{token}/result",
                headers=HEADERS,
            )
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "error": _response_error(resp)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─── Processar lote ──────────────────────────────────────────

def processar_lote(
    videos: list[tuple[bytes, str]],
    account_id: str = "default",
    cfg: dict = None,
) -> dict:
    """
    videos: lista de (bytes, filename)
    Retorna dict com resultados por arquivo.
    """
    try:
        _check_configured()
        config_json = json.dumps(cfg or {})
        files = [("videos", (fname, vbytes, "video/mp4")) for vbytes, fname in videos]
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                f"{VIDEO_API_URL}/api/v1/processar/lote",
                headers=HEADERS,
                files=files,
                data={"account_id": account_id, "config_json": config_json},
            )
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "error": _response_error(resp)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Download de lote ────────────────────────────────────────

def download_lote_video(job_id: str) -> bytes | None:
    try:
        _check_configured()
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{VIDEO_API_URL}/api/v1/download/{job_id}",
                headers=HEADERS,
            )
        if resp.status_code == 200:
            return resp.content
    except Exception:
        logger.warning("Download do lote falhou para o job %s.", job_id)
    return None


# ─── Status da API ───────────────────────────────────────────

def api_status() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(f"{VIDEO_API_URL}/api/v1/status", headers=HEADERS)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Config default ──────────────────────────────────────────

def config_default() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(f"{VIDEO_API_URL}/api/v1/config/default", headers=HEADERS)
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "error": _response_error(resp)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def limpar_tmp() -> dict:
    try:
        _check_configured()
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.delete(f"{VIDEO_API_URL}/api/v1/limpar", headers=HEADERS)
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "error": _response_error(resp)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
