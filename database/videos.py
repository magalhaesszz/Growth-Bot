import hashlib
import logging
import re
from pathlib import Path

from supabase import create_client

from config import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)


class VideoDB:
    BUCKET = "videos"

    def __init__(self):
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename or "video.mp4").name
        clean = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
        return clean or "video.mp4"

    # ─── Storage ─────────────────────────────────────────────

    def upload_video(self, video_bytes: bytes, filename: str, user_id: int) -> str | None:
        """Upload idempotente: o mesmo arquivo gera o mesmo storage_path."""
        try:
            digest = hashlib.sha256(video_bytes).hexdigest()[:32]
            safe_name = self._safe_filename(filename)
            path = f"{int(user_id)}/{digest}_{safe_name}"
            self.sb.storage.from_(self.BUCKET).upload(
                path,
                video_bytes,
                {"content-type": "video/mp4", "upsert": "true"},
            )
            return path
        except Exception as exc:
            logger.error("Erro ao fazer upload do video: %s", exc)
            return None

    def get_video_url(self, storage_path: str) -> str | None:
        try:
            return self.sb.storage.from_(self.BUCKET).get_public_url(storage_path)
        except Exception as exc:
            logger.error("Erro ao gerar URL do video: %s", exc)
            return None

    def download_video(self, storage_path: str) -> bytes | None:
        try:
            return self.sb.storage.from_(self.BUCKET).download(storage_path)
        except Exception as exc:
            logger.error("Erro ao baixar video do Storage: %s", exc)
            return None

    def delete_video(self, storage_path: str):
        try:
            self.sb.storage.from_(self.BUCKET).remove([storage_path])
        except Exception as exc:
            logger.error("Erro ao deletar video: %s", exc)

    # ─── Banco ───────────────────────────────────────────────

    def save_video(
        self,
        user_id: int,
        filename: str,
        storage_path: str,
        source_url: str = "",
        size_mb: float = 0.0,
    ) -> dict:
        # O handler antigo podia chamar upload/save duas vezes. Dedupe aqui torna
        # esse caminho seguro sem criar dois registros ou dois objetos.
        existing = (
            self.sb.table("videos")
            .select("*")
            .eq("user_id", int(user_id))
            .eq("storage_path", storage_path)
            .neq("status", "deleted")
            .limit(1)
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            updates = {
                "filename": self._safe_filename(filename),
                "size_mb": float(size_mb or 0.0),
            }
            if source_url:
                updates["source_url"] = source_url
            self.sb.table("videos").update(updates).eq("id", row["id"]).execute()
            row.update(updates)
            return row

        row = {
            "user_id": int(user_id),
            "filename": self._safe_filename(filename),
            "source_url": source_url,
            "storage_path": storage_path,
            "size_mb": float(size_mb or 0.0),
            "status": "downloaded",
        }
        res = self.sb.table("videos").insert(row).execute()
        return res.data[0] if res.data else {}

    def list_videos(self, user_id: int, limit: int = 20) -> list[dict]:
        res = (
            self.sb.table("videos")
            .select("*")
            .eq("user_id", int(user_id))
            .neq("status", "deleted")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def get_video(self, video_id: str, user_id: int | None = None) -> dict | None:
        query = self.sb.table("videos").select("*").eq("id", video_id)
        if user_id is not None:
            query = query.eq("user_id", int(user_id))
        res = query.limit(1).execute()
        return res.data[0] if res.data else None

    def update_status(self, video_id: str, status: str):
        self.sb.table("videos").update({"status": status}).eq("id", video_id).execute()

    # ─── Fundo principal ─────────────────────────────────────

    def save_fundo(self, user_id: int, fundo_bytes: bytes, filename: str) -> str | None:
        path = f"fundos/{int(user_id)}/fundo.png"
        try:
            try:
                self.sb.storage.from_(self.BUCKET).remove([path])
            except Exception:
                pass
            self.sb.storage.from_(self.BUCKET).upload(
                path,
                fundo_bytes,
                {"content-type": "image/png", "upsert": "true"},
            )
            self.sb.table("config_fundo").upsert(
                {
                    "user_id": int(user_id),
                    "storage_path": path,
                    "filename": filename,
                },
                on_conflict="user_id",
            ).execute()
            return path
        except Exception as exc:
            logger.error("Erro ao salvar fundo: %s", exc)
            return None

    def get_fundo(self, user_id: int) -> bytes | None:
        try:
            res = (
                self.sb.table("config_fundo")
                .select("storage_path")
                .eq("user_id", int(user_id))
                .limit(1)
                .execute()
            )
            if not res.data:
                return None
            return self.sb.storage.from_(self.BUCKET).download(
                res.data[0]["storage_path"]
            )
        except Exception as exc:
            logger.error("Erro ao buscar fundo: %s", exc)
            return None

    # ─── Multiplos fundos ────────────────────────────────────

    def save_fundo_named(
        self, user_id: int, fundo_bytes: bytes, filename: str, nome: str
    ) -> str | None:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", nome.replace(" ", "_"))[:30] or "fundo"
        path = f"fundos/{int(user_id)}/{slug}.png"
        try:
            try:
                self.sb.storage.from_(self.BUCKET).remove([path])
            except Exception:
                pass
            self.sb.storage.from_(self.BUCKET).upload(
                path,
                fundo_bytes,
                {"content-type": "image/png", "upsert": "true"},
            )
            self.sb.table("config_fundos").upsert(
                {
                    "user_id": int(user_id),
                    "slug": slug,
                    "nome": nome,
                    "storage_path": path,
                    "filename": filename,
                },
                on_conflict="user_id,slug",
            ).execute()
            return path
        except Exception as exc:
            logger.error("Erro ao salvar fundo nomeado: %s", exc)
            return None

    def list_fundos(self, user_id: int) -> list[dict]:
        try:
            res = (
                self.sb.table("config_fundos")
                .select("*")
                .eq("user_id", int(user_id))
                .order("created_at", desc=False)
                .execute()
            )
            return res.data or []
        except Exception as exc:
            logger.error("Erro ao listar fundos: %s", exc)
            return []

    def set_fundo_ativo(self, user_id: int, slug: str) -> bool:
        try:
            res = (
                self.sb.table("config_fundos")
                .select("*")
                .eq("user_id", int(user_id))
                .eq("slug", slug)
                .limit(1)
                .execute()
            )
            if not res.data:
                return False
            fundo = res.data[0]
            data = self.sb.storage.from_(self.BUCKET).download(fundo["storage_path"])
            return bool(self.save_fundo(user_id, data, fundo["filename"]))
        except Exception as exc:
            logger.error("Erro ao ativar fundo: %s", exc)
            return False

    def delete_fundo_named(self, user_id: int, slug: str):
        try:
            path = f"fundos/{int(user_id)}/{slug}.png"
            self.sb.storage.from_(self.BUCKET).remove([path])
            self.sb.table("config_fundos").delete().eq("user_id", int(user_id)).eq(
                "slug", slug
            ).execute()
        except Exception as exc:
            logger.error("Erro ao remover fundo nomeado: %s", exc)

    def get_fundo_info(self, user_id: int) -> dict | None:
        try:
            res = (
                self.sb.table("config_fundo")
                .select("*")
                .eq("user_id", int(user_id))
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception:
            return None

    def delete_record(self, video_id: str):
        self.sb.table("videos").update({"status": "deleted"}).eq("id", video_id).execute()
