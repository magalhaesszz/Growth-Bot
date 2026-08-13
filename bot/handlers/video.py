import json
import logging
import os

from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters,
)

from config import TELEGRAM_OWNER_ID
import video_client as vc

logger = logging.getLogger(__name__)

# Estado da conversa
AGUARDANDO_FUNDO  = 10
AGUARDANDO_VIDEO  = 11
AGUARDANDO_LOTE   = 12

# Config de vídeo por usuário (em memória — persiste enquanto bot rodar)
_user_cfg: dict[int, dict] = {}


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            await update.message.reply_text("⛔ Acesso negado.")
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


def _account_id(update: Update) -> str:
    return str(update.effective_user.id)


def _cfg(update: Update) -> dict:
    return _user_cfg.get(update.effective_user.id, {})


# ─── /fundo ──────────────────────────────────────────────────

async def cmd_fundo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("⛔ Acesso negado.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🖼 *Envie a imagem de fundo* (PNG ou JPG, exatamente *1080x1920px*).\n\n"
        "Ela será usada como base em todos os vídeos processados.",
        parse_mode="Markdown"
    )
    return AGUARDANDO_FUNDO


async def receber_fundo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    photo = msg.photo or (msg.document if msg.document and msg.document.mime_type.startswith("image") else None)

    if not photo:
        await msg.reply_text("❌ Envie uma imagem (PNG ou JPG).")
        return AGUARDANDO_FUNDO

    file_obj = await (photo[-1] if isinstance(photo, list) else photo).get_file()
    fundo_bytes = await file_obj.download_as_bytearray()
    filename = getattr(msg.document, "file_name", "fundo.png") if msg.document else "fundo.png"

    await msg.reply_text("⏳ Salvando fundo...")

    result = vc.salvar_fundo(bytes(fundo_bytes), filename, _account_id(update))
    if result["ok"]:
        await msg.reply_text("✅ Fundo salvo com sucesso!\nAgora use /video para processar um vídeo.")
    else:
        await msg.reply_text(f"❌ {result['error']}")

    return ConversationHandler.END


# ─── /video ──────────────────────────────────────────────────

async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("⛔ Acesso negado.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🎬 *Envie o vídeo .mp4* que deseja editar.\n\n"
        "O bot vai processar com anti-ban e te devolver o resultado.\n"
        "Use /cancelar para sair.",
        parse_mode="Markdown"
    )
    return AGUARDANDO_VIDEO


async def receber_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    vid = msg.video

    file_ref = None
    filename  = "video.mp4"

    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref = doc
        filename  = doc.file_name
    elif vid:
        file_ref = vid
        filename  = f"video_{vid.file_unique_id}.mp4"
    else:
        await msg.reply_text("❌ Envie um arquivo .mp4.")
        return AGUARDANDO_VIDEO

    await msg.reply_text("⏳ Recebendo e processando vídeo... Aguarde.")

    file_obj    = await file_ref.get_file()
    video_bytes = await file_obj.download_as_bytearray()

    result = vc.processar_video(
        video_bytes=bytes(video_bytes),
        filename=filename,
        account_id=_account_id(update),
        cfg=_cfg(update),
    )

    if result["ok"]:
        await msg.reply_video(
            video=result["video_bytes"],
            filename=result["filename"],
            caption=(
                f"✅ *Vídeo editado!*\n"
                f"Tamanho: {result['size_mb']} MB"
            ),
            parse_mode="Markdown"
        )
    else:
        await msg.reply_text(f"❌ Erro no processamento:\n`{result['error']}`", parse_mode="Markdown")

    return ConversationHandler.END


# ─── /video_lote ─────────────────────────────────────────────

async def cmd_video_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("⛔ Acesso negado.")
        return ConversationHandler.END

    ctx.user_data["lote_videos"] = []
    await update.message.reply_text(
        "📦 *Modo lote ativado!*\n\n"
        "Envie os vídeos .mp4 um a um (máximo 10).\n"
        "Quando terminar, envie /processar_lote para processar todos.",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LOTE


async def coletar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg    = update.message
    doc    = msg.document
    vid    = msg.video
    lote   = ctx.user_data.get("lote_videos", [])

    if len(lote) >= 10:
        await msg.reply_text("⚠️ Máximo de 10 vídeos. Use /processar_lote para processar.")
        return AGUARDANDO_LOTE

    file_ref = None
    filename  = "video.mp4"
    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref = doc
        filename  = doc.file_name
    elif vid:
        file_ref = vid
        filename  = f"video_{vid.file_unique_id}.mp4"

    if not file_ref:
        await msg.reply_text("❌ Envie apenas arquivos .mp4 ou use /processar_lote.")
        return AGUARDANDO_LOTE

    file_obj    = await file_ref.get_file()
    video_bytes = await file_obj.download_as_bytearray()
    lote.append((bytes(video_bytes), filename))
    ctx.user_data["lote_videos"] = lote

    await msg.reply_text(
        f"✅ Vídeo {len(lote)}/10 recebido: *{filename}*\n"
        f"Envie mais ou use /processar_lote para processar.",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LOTE


async def executar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lote = ctx.user_data.get("lote_videos", [])
    if not lote:
        await update.message.reply_text("Nenhum vídeo na fila.")
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Processando {len(lote)} vídeo(s)...")

    result = vc.processar_lote(lote, _account_id(update), _cfg(update))

    if not result.get("resultados"):
        await update.message.reply_text(f"❌ Erro: {result.get('error')}")
        return ConversationHandler.END

    for item in result["resultados"]:
        if item["ok"]:
            video_bytes = vc.download_lote_video(item["job_id"])
            if video_bytes:
                await update.message.reply_video(
                    video=video_bytes,
                    filename=f"editado_{item['arquivo']}",
                    caption=f"✅ *{item['arquivo']}* — {item['size_mb']} MB",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"⚠️ {item['arquivo']}: processado mas download falhou.")
        else:
            await update.message.reply_text(f"❌ *{item['arquivo']}*: {item['error']}", parse_mode="Markdown")

    ctx.user_data.pop("lote_videos", None)
    return ConversationHandler.END


# ─── /video_status ───────────────────────────────────────────

@owner_only
async def cmd_video_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Consultando API...")
    status = vc.api_status()
    if status.get("ok"):
        await update.message.reply_text(
            f"📡 *Status da Video API*\n\n"
            f"FFmpeg: {'✅' if status.get('ffmpeg') else '❌'}\n"
            f"Fundos cadastrados: {status.get('fundos_cadastrados', 0)}\n"
            f"Contas: {', '.join(status.get('contas', [])) or 'nenhuma'}\n"
            f"Vídeos na fila: {status.get('videos_em_fila', 0)}\n"
            f"Vídeos prontos: {status.get('videos_prontos', 0)}\n"
            f"Disco livre /tmp: {status.get('disco_tmp_livre_mb', '?')} MB",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ API offline: {status.get('error')}")


# ─── /config_video ───────────────────────────────────────────

@owner_only
async def cmd_config_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = _user_cfg.get(uid, {})

    if not ctx.args:
        defaults = vc.config_default()
        atual = {**defaults, **cfg}
        linhas = [
            "⚙️ *Configurações do editor de vídeo:*\n",
            f"  Largura do vídeo: `{atual.get('video_width', 800)}px`",
            f"  Posição vertical: `{atual.get('position_y', 0.25)} (0=topo, 1=base)`",
            f"  Qualidade (CRF): `{atual.get('output_crf', 18)}` (18=alta, 28=baixa)",
            f"  Anti-ban: `{'ativado' if atual.get('antiban', True) else 'desativado'}`",
            f"  Fix mirror: `{'sim' if atual.get('fix_mirror', False) else 'não'}`",
            f"  FPS: `{atual.get('output_fps', 30)}`",
            f"\nUso: /config_video chave=valor",
            f"Exemplos:",
            f"`/config_video video_width=900`",
            f"`/config_video position_y=0.30`",
            f"`/config_video output_crf=23`",
            f"`/config_video antiban=false`",
            f"`/config_video fix_mirror=true`",
        ]
        await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")
        return

    # Aplicar configuração
    for arg in ctx.args:
        if "=" not in arg:
            continue
        k, v = arg.split("=", 1)
        k = k.strip()
        v = v.strip()

        # Conversão de tipos
        if v.lower() in ("true", "false"):
            cfg[k] = v.lower() == "true"
        elif "." in v:
            try: cfg[k] = float(v)
            except: cfg[k] = v
        else:
            try: cfg[k] = int(v)
            except: cfg[k] = v

    _user_cfg[uid] = cfg
    await update.message.reply_text(
        f"✅ Configuração atualizada:\n" +
        "\n".join(f"  `{k}` = `{v}`" for k, v in cfg.items()),
        parse_mode="Markdown"
    )


# ─── /config_video_reset ─────────────────────────────────────

@owner_only
async def cmd_config_video_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _user_cfg.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ Configurações de vídeo resetadas para o padrão.")


# ─── /video_limpar ───────────────────────────────────────────

@owner_only
async def cmd_video_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = vc.limpar_tmp()
    if result.get("ok"):
        await update.message.reply_text(f"🗑 {result.get('removidos', 0)} arquivo(s) removido(s) do servidor.")
    else:
        await update.message.reply_text(f"❌ Erro: {result.get('error')}")


# ─── /cancelar ───────────────────────────────────────────────

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END


# ─── Registro ────────────────────────────────────────────────

def register_video_handlers(app):
    # Conversa /fundo
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("fundo", cmd_fundo)],
        states={
            AGUARDANDO_FUNDO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receber_fundo),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
    ))

    # Conversa /video (único)
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("video", cmd_video)],
        states={
            AGUARDANDO_VIDEO: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_video),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
    ))

    # Conversa /video_lote
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("video_lote", cmd_video_lote)],
        states={
            AGUARDANDO_LOTE: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, coletar_lote),
                CommandHandler("processar_lote", executar_lote),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
    ))

    # Comandos simples
    app.add_handler(CommandHandler("video_status",       cmd_video_status))
    app.add_handler(CommandHandler("config_video",       cmd_config_video))
    app.add_handler(CommandHandler("config_video_reset", cmd_config_video_reset))
    app.add_handler(CommandHandler("video_limpar",       cmd_video_limpar))
