"""
Gerencia a tabela de vídeos no Supabase.

Tabela: videos
- id          UUID PK
- user_id     BIGINT
- filename    TEXT
- source_url  TEXT (link original Instagram/TikTok)
- storage_path TEXT (caminho no Supabase Storage)
- size_mb     FLOAT
- status      TEXT (downloaded | processed | error)
- created_at  TIMESTAMPTZ
"""
import logging
import uuid
from datetime import datetime, timezone

from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)


class VideoDB:
    BUCKET = "videos"

    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ─── Storage ─────────────────────────────────────────────

    def upload_video(self, video_bytes: bytes, filename: str, user_id: int) -> str | None:
        """Faz upload para o Supabase Storage e retorna o path."""
        try:
            path = f"{user_id}/{uuid.uuid4().hex}_{filename}"
            self.sb.storage.from_(self.BUCKET).upload(
                path, video_bytes,
                {"content-type": "video/mp4", "upsert": "true"}
            )
            return path
        except Exception as e:
            logger.error(f"Erro ao fazer upload do vídeo: {e}")
            return None

    def get_video_url(self, storage_path: str) -> str | None:
        """Retorna URL pública do vídeo."""
        try:
            res = self.sb.storage.from_(self.BUCKET).get_public_url(storage_path)
            return res
        except Exception as e:
            logger.error(f"Erro ao gerar URL do vídeo: {e}")
            return None

    def download_video(self, storage_path: str) -> bytes | None:
        """Baixa vídeo do Supabase Storage."""
        try:
            res = self.sb.storage.from_(self.BUCKET).download(storage_path)
            return res
        except Exception as e:
            logger.error(f"Erro ao baixar vídeo do Storage: {e}")
            return None

    def delete_video(self, storage_path: str):
        try:
            self.sb.storage.from_(self.BUCKET).remove([storage_path])
        except Exception as e:
            logger.error(f"Erro ao deletar vídeo: {e}")

    # ─── Banco ───────────────────────────────────────────────

    def save_video(self, user_id: int, filename: str, storage_path: str,
                   source_url: str = "", size_mb: float = 0.0) -> dict:
        row = {
            "user_id":      user_id,
            "filename":     filename,
            "source_url":   source_url,
            "storage_path": storage_path,
            "size_mb":      size_mb,
            "status":       "downloaded",
        }
        res = self.sb.table("videos").insert(row).execute()
        return res.data[0] if res.data else {}

    def list_videos(self, user_id: int, limit: int = 20) -> list[dict]:
        res = (
            self.sb.table("videos")
            .select("*")
            .eq("user_id", user_id)
            .neq("status", "deleted")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def get_video(self, video_id: str) -> dict | None:
        res = self.sb.table("videos").select("*").eq("id", video_id).execute()
        return res.data[0] if res.data else None

    def update_status(self, video_id: str, status: str):
        self.sb.table("videos").update({"status": status}).eq("id", video_id).execute()

    # ─── Fundo ───────────────────────────────────────────────

    def save_fundo(self, user_id: int, fundo_bytes: bytes, filename: str) -> str | None:
        """Salva fundo no Storage, substituindo o anterior."""
        path = f"fundos/{user_id}/fundo.png"
        try:
            # Tenta remover o anterior antes de fazer upload
            try:
                self.sb.storage.from_(self.BUCKET).remove([path])
            except Exception:
                pass
            self.sb.storage.from_(self.BUCKET).upload(
                path, fundo_bytes,
                {"content-type": "image/png", "upsert": "true"}
            )
            # Registrar na tabela config_fundo
            self.sb.table("config_fundo").upsert({
                "user_id":      user_id,
                "storage_path": path,
                "filename":     filename,
            }).execute()
            return path
        except Exception as e:
            logger.error(f"Erro ao salvar fundo: {e}")
            return None

    def get_fundo(self, user_id: int) -> bytes | None:
        """Busca o fundo atual do Storage."""
        try:
            res = self.sb.table("config_fundo").select("storage_path")                .eq("user_id", user_id).execute()
            if not res.data:
                return None
            path = res.data[0]["storage_path"]
            return self.sb.storage.from_(self.BUCKET).download(path)
        except Exception as e:
            logger.error(f"Erro ao buscar fundo: {e}")
            return None

    # ─── Multiplos fundos ────────────────────────────────────

    def save_fundo_named(self, user_id: int, fundo_bytes: bytes, filename: str, nome: str) -> str | None:
        """Salva um fundo nomeado (ate 5 por usuario), sem apagar os outros."""
        import re
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", nome.replace(" ", "_"))[:30] or "fundo"
        path = f"fundos/{user_id}/{slug}.png"
        try:
            try:
                self.sb.storage.from_(self.BUCKET).remove([path])
            except Exception:
                pass
            self.sb.storage.from_(self.BUCKET).upload(
                path, fundo_bytes,
                {"content-type": "image/png", "upsert": "true"}
            )
            self.sb.table("config_fundos").upsert({
                "user_id": user_id, "slug": slug,
                "nome": nome, "storage_path": path, "filename": filename,
            }, on_conflict="user_id,slug").execute()
            return path
        except Exception as e:
            logger.error(f"Erro ao salvar fundo nomeado: {e}")
            return None

    def list_fundos(self, user_id: int) -> list[dict]:
        """Lista todos os fundos salvos do usuario."""
        try:
            res = (self.sb.table("config_fundos")
                   .select("*").eq("user_id", user_id)
                   .order("created_at", desc=False).execute())
            return res.data or []
        except Exception as e:
            logger.error(f"Erro ao listar fundos: {e}")
            return []

    def set_fundo_ativo(self, user_id: int, slug: str) -> bool:
        """Marca um fundo como ativo — sera usado ao processar. Copia para o slot principal."""
        try:
            res = (self.sb.table("config_fundos")
                   .select("*").eq("user_id", user_id).eq("slug", slug).execute())
            if not res.data:
                return False
            fundo = res.data[0]
            data = self.sb.storage.from_(self.BUCKET).download(fundo["storage_path"])
            self.save_fundo(user_id, data, fundo["filename"])
            return True
        except Exception as e:
            logger.error(f"Erro ao ativar fundo: {e}")
            return False

    def delete_fundo_named(self, user_id: int, slug: str):
        try:
            path = f"fundos/{user_id}/{slug}.png"
            self.sb.storage.from_(self.BUCKET).remove([path])
            self.sb.table("config_fundos").delete().eq("user_id", user_id).eq("slug", slug).execute()
        except Exception as e:
            logger.error(f"Erro ao remover fundo nomeado: {e}")


    def get_fundo_info(self, user_id: int) -> dict | None:
        """Retorna info do fundo cadastrado."""
        try:
            res = self.sb.table("config_fundo").select("*")                .eq("user_id", user_id).execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def delete_record(self, video_id: str):
        self.sb.table("videos").update({"status": "deleted"}).eq("id", video_id).execute()
