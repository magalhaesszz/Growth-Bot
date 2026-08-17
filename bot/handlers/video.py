import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ConversationHandler,
    ContextTypes, MessageHandler, filters,
)

from config import TELEGRAM_OWNER_ID
import video_client as vc
import video_settings

logger = logging.getLogger(__name__)

# ─── Estados ─────────────────────────────────────────────────
AGUARDANDO_FUNDO     = 10
AGUARDANDO_VIDEO     = 11
AGUARDANDO_LOTE      = 12
AGUARDANDO_LINK      = 15
AGUARDANDO_WATERMARK = 16
AGUARDANDO_CAPTION   = 17
AGUARDANDO_CROP      = 18
AGUARDANDO_FUNDO_DL  = 19


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            if update.message:
                await update.message.reply_text("⛔ Acesso negado.")
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


def _uid(update: Update) -> str:
    return str(update.effective_user.id)


def _cfg(update: Update) -> dict:
    return video_settings.get_config(update.effective_user.id)


def _menu_edicao_kb(edits: dict) -> InlineKeyboardMarkup:
    """Teclado do menu de edições com badge ✅ quando configurado."""
    wm  = "💧✅" if edits.get("watermark") else "💧"
    cap = "📝✅" if edits.get("caption")   else "📝"
    cr  = "✂️✅" if edits.get("crop_start") else "✂️"
    fd  = "🎨✅" if edits.get("fundo")      else "🎨"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{fd} Fundo",       callback_data="dl:fundo"),
         InlineKeyboardButton(f"{cr} Cortar",      callback_data="dl:crop")],
        [InlineKeyboardButton(f"{wm} Marca d'água",callback_data="dl:watermark"),
         InlineKeyboardButton(f"{cap} Legenda",    callback_data="dl:caption")],
        [InlineKeyboardButton("▶️ Processar tudo", callback_data="dl:processar")],
        [InlineKeyboardButton("❌ Cancelar",       callback_data="dl:cancelar")],
    ])


async def _show_edicao_menu(msg, filename: str, edits: dict, extra: str = ""):
    text = (
        f"🎬 *{filename}*\n\n"
        f"Escolha as edições antes de processar:"
    )
    if extra:
        text += f"\n_{extra}_"
    await msg.reply_text(text, reply_markup=_menu_edicao_kb(edits), parse_mode="Markdown")


# ─── /download — link Instagram ou TikTok ────────────────────

@owner_only
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 *Envie o link do vídeo*\n\n"
        "Suportado: Instagram e TikTok\n"
        "_Use /cancelar para sair._",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LINK


async def receber_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Envie um link válido começando com http(s)://")
        return AGUARDANDO_LINK

    msg = await update.message.reply_text("⏳ Baixando vídeo... Aguarde.")
    result = await asyncio.to_thread(vc.download_link, url)

    if not result.get("ok"):
        await msg.edit_text(f"❌ Falha no download:\n`{result.get('error', 'Erro desconhecido')}`",
                            parse_mode="Markdown")
        return ConversationHandler.END

    ctx.user_data["dl_job_id"]   = result["job_id"]
    ctx.user_data["dl_filename"] = result["filename"]
    ctx.user_data["dl_edits"]    = {}

    await msg.edit_text(f"✅ Baixado! ({result['size_mb']} MB)")
    await _show_edicao_menu(update.message, result["filename"], {})
    return AGUARDANDO_LINK


# ─── /video — envio direto de arquivo ────────────────────────

@owner_only
async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dl_edits"]    = {}
    ctx.user_data["dl_job_id"]   = None
    ctx.user_data["dl_filename"] = None
    await update.message.reply_text(
        "🎬 *Envie o vídeo .mp4* para editar.\n_Use /cancelar para sair._",
        parse_mode="Markdown"
    )
    return AGUARDANDO_VIDEO


async def receber_video_arquivo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    vid = msg.video
    file_ref, filename = None, "video.mp4"

    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref, filename = doc, doc.file_name
    elif vid:
        file_ref, filename = vid, f"video_{vid.file_unique_id}.mp4"
    else:
        await msg.reply_text("❌ Envie um arquivo .mp4.")
        return AGUARDANDO_VIDEO

    status = await msg.reply_text("⏳ Recebendo vídeo...")
    file_obj    = await file_ref.get_file()
    video_bytes = bytes(await file_obj.download_as_bytearray())

    ctx.user_data["dl_video_bytes"] = video_bytes
    ctx.user_data["dl_filename"]    = filename
    ctx.user_data["dl_job_id"]      = None
    ctx.user_data["dl_edits"]       = {}

    await status.edit_text(f"✅ Vídeo recebido! ({round(len(video_bytes)/1048576,1)} MB)")
    await _show_edicao_menu(msg, filename, {})
    return AGUARDANDO_LINK


# ─── Callbacks dos botões de edição ──────────────────────────

async def on_dl_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.replace("dl:", "")
    edits  = ctx.user_data.setdefault("dl_edits", {})
    fname  = ctx.user_data.get("dl_filename", "video.mp4")

    if action == "cancelar":
        ctx.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END

    if action == "fundo":
        await query.edit_message_text(
            "🎨 *Envie a imagem de fundo*\n\nPNG ou JPG, ideal 1080x1920px.\n"
            "Ou envie /pular para continuar sem fundo.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_FUNDO_DL

    if action == "watermark":
        await query.edit_message_text(
            "💧 *Marca d'água*\n\nDigite o texto (ex: @usuario):\n"
            "Ou envie /pular para não adicionar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_WATERMARK

    if action == "caption":
        await query.edit_message_text(
            "📝 *Legenda*\n\nDigite o texto para a parte inferior:\n"
            "Ou envie /pular para não adicionar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CAPTION

    if action == "crop":
        await query.edit_message_text(
            "✂️ *Cortar vídeo*\n\nDigite início e fim em segundos (ex: `5-30`):\n"
            "Ou envie /pular para não cortar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CROP

    if action == "processar":
        await query.edit_message_text("⏳ Processando vídeo com as edições...")
        result = await _processar(update, ctx)
        if result.get("ok"):
            await query.message.reply_video(
                video=result["video_bytes"],
                filename=result["filename"],
                caption=f"✅ *Vídeo pronto!* {result['size_mb']} MB",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(
                f"❌ Erro: `{result.get('error')}`", parse_mode="Markdown")
        ctx.user_data.clear()
        return ConversationHandler.END

    return AGUARDANDO_LINK


async def _processar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Baixa (se necessário) e aplica todas as edições acumuladas."""
    job_id  = ctx.user_data.get("dl_job_id")
    fname   = ctx.user_data.get("dl_filename", "video.mp4")
    edits   = ctx.user_data.get("dl_edits", {})
    vid_bytes = ctx.user_data.get("dl_video_bytes")

    # Obter bytes do vídeo
    if not vid_bytes:
        if not job_id:
            return {"ok": False, "error": "Nenhum vídeo disponível."}
        vid_bytes = await asyncio.to_thread(vc.buscar_video_baixado, job_id)
        if not vid_bytes:
            return {"ok": False, "error": "Não foi possível recuperar o vídeo do servidor."}

    wm     = edits.get("watermark", "")
    cap    = edits.get("caption", "")
    crop_s = edits.get("crop_start", 0.0)
    crop_e = edits.get("crop_end", 0.0)

    # Se tem edições de texto/corte — usar /editar
    if wm or cap or crop_s or crop_e:
        result = await asyncio.to_thread(
            vc.editar_video, vid_bytes, fname, wm, cap, crop_s, crop_e
        )
        if not result.get("ok"):
            return result
        vid_bytes = result["video_bytes"]
        fname     = result["filename"]

    # Aplicar fundo (processamento de fundo 9:16)
    result = await asyncio.to_thread(
        vc.processar_video, vid_bytes, fname, _uid(update), _cfg(update)
    )
    return result


# ─── Receber inputs de edição ─────────────────────────────────

async def receber_fundo_dl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    text  = (msg.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})

    if text == "/pular":
        await msg.reply_text("⏭ Sem fundo.")
        await _show_edicao_menu(msg, ctx.user_data.get("dl_filename","video.mp4"), edits)
        return AGUARDANDO_LINK

    photo = msg.photo or (
        msg.document if msg.document and
        msg.document.mime_type and
        msg.document.mime_type.startswith("image") else None
    )
    if not photo:
        await msg.reply_text("❌ Envie uma imagem PNG/JPG ou /pular.")
        return AGUARDANDO_FUNDO_DL

    file_obj    = await (photo[-1] if isinstance(photo, list) else photo).get_file()
    fundo_bytes = bytes(await file_obj.download_as_bytearray())
    fname_fundo = getattr(msg.document, "file_name", "fundo.png") if msg.document else "fundo.png"

    result = await asyncio.to_thread(vc.salvar_fundo, fundo_bytes, fname_fundo, _uid(update))
    if result.get("ok"):
        edits["fundo"] = True
        await msg.reply_text("✅ Fundo salvo!")
    else:
        await msg.reply_text(f"❌ Erro: {result.get('error')}")

    await _show_edicao_menu(msg, ctx.user_data.get("dl_filename","video.mp4"), edits)
    return AGUARDANDO_LINK


async def receber_watermark(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if text and text != "/pular":
        edits["watermark"] = text
        extra = f"Marca d'água: {text}"
    else:
        extra = "Sem marca d'água."
    await _show_edicao_menu(update.message,
        ctx.user_data.get("dl_filename","video.mp4"), edits, extra)
    return AGUARDANDO_LINK


async def receber_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if text and text != "/pular":
        edits["caption"] = text
        extra = f"Legenda: {text}"
    else:
        extra = "Sem legenda."
    await _show_edicao_menu(update.message,
        ctx.user_data.get("dl_filename","video.mp4"), edits, extra)
    return AGUARDANDO_LINK


async def receber_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if text and text != "/pular" and "-" in text:
        try:
            s, e = text.split("-", 1)
            edits["crop_start"] = float(s)
            edits["crop_end"]   = float(e)
            extra = f"Corte: {s}s até {e}s"
        except Exception:
            await update.message.reply_text("❌ Formato inválido. Use: `5-30`",
                                            parse_mode="Markdown")
            return AGUARDANDO_CROP
    else:
        extra = "Sem corte."
    await _show_edicao_menu(update.message,
        ctx.user_data.get("dl_filename","video.mp4"), edits, extra)
    return AGUARDANDO_LINK


# ─── /video_lote ─────────────────────────────────────────────

@owner_only
async def cmd_video_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["lote_videos"] = []
    await update.message.reply_text(
        "📦 *Modo lote ativado!*\n\nEnvie os vídeos .mp4 um a um (máx 10).\n"
        "Quando terminar, envie /processar_lote.",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LOTE


async def coletar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, doc, vid = update.message, update.message.document, update.message.video
    lote = ctx.user_data.get("lote_videos", [])
    if len(lote) >= 10:
        await msg.reply_text("⚠️ Máximo de 10 vídeos. Use /processar_lote.")
        return AGUARDANDO_LOTE
    file_ref, filename = None, "video.mp4"
    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref, filename = doc, doc.file_name
    elif vid:
        file_ref, filename = vid, f"video_{vid.file_unique_id}.mp4"
    if not file_ref:
        await msg.reply_text("❌ Envie apenas .mp4 ou use /processar_lote.")
        return AGUARDANDO_LOTE
    vb = bytes(await (await file_ref.get_file()).download_as_bytearray())
    lote.append((vb, filename))
    ctx.user_data["lote_videos"] = lote
    await msg.reply_text(f"✅ {len(lote)}/10 — *{filename}*\nEnvie mais ou /processar_lote.",
                         parse_mode="Markdown")
    return AGUARDANDO_LOTE


async def executar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lote = ctx.user_data.get("lote_videos", [])
    if not lote:
        await update.message.reply_text("Nenhum vídeo na fila.")
        return ConversationHandler.END
    await update.message.reply_text(f"⏳ Processando {len(lote)} vídeo(s)...")
    result = await asyncio.to_thread(vc.processar_lote, lote, _uid(update), _cfg(update))
    if not result.get("resultados"):
        await update.message.reply_text(f"❌ {result.get('error')}")
        return ConversationHandler.END
    for item in result["resultados"]:
        if item["ok"]:
            vb = await asyncio.to_thread(vc.download_lote_video, item["job_id"])
            if vb:
                await update.message.reply_video(video=vb,
                    filename=f"editado_{item['arquivo']}",
                    caption=f"✅ *{item['arquivo']}* — {item['size_mb']} MB",
                    parse_mode="Markdown")
            else:
                await update.message.reply_text(f"⚠️ {item['arquivo']}: download falhou.")
        else:
            await update.message.reply_text(f"❌ *{item['arquivo']}*: {item['error']}",
                                            parse_mode="Markdown")
    ctx.user_data.pop("lote_videos", None)
    return ConversationHandler.END


# ─── /video_status ───────────────────────────────────────────

@owner_only
async def cmd_video_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = await asyncio.to_thread(vc.api_status)
    if status.get("ok"):
        await update.message.reply_text(
            f"📡 *Status da Video API*\n\n"
            f"FFmpeg: {'✅' if status.get('ffmpeg') else '❌'}\n"
            f"Fundos: {status.get('fundos_cadastrados',0)}\n"
            f"Fila: {status.get('videos_em_fila',0)} | Prontos: {status.get('videos_prontos',0)}\n"
            f"Disco /tmp: {status.get('disco_tmp_livre_mb','?')} MB livres",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ API offline: {status.get('error')}")


# ─── /config_video ───────────────────────────────────────────

@owner_only
async def cmd_config_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = video_settings.get_config(uid)
    if not ctx.args:
        await update.message.reply_text(
            f"⚙️ *Config do editor:*\n\n"
            f"Largura: `{cfg.get('video_width',800)}px`\n"
            f"Pos. vertical: `{cfg.get('position_y',0.25)}`\n"
            f"CRF: `{cfg.get('output_crf',18)}`\n"
            f"FPS: `{cfg.get('output_fps',30)}`\n"
            f"Anti-ban: `{cfg.get('antiban',True)}`\n\n"
            f"Uso: `/config_video chave=valor`",
            parse_mode="Markdown"
        )
        return
    for arg in ctx.args:
        if "=" not in arg:
            continue
        k, v = arg.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.lower() in ("true","false"):
            cfg[k] = v.lower() == "true"
        elif "." in v:
            try: cfg[k] = float(v)
            except: cfg[k] = v
        else:
            try: cfg[k] = int(v)
            except: cfg[k] = v
    try:
        cfg = video_settings.set_values(uid, cfg)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await update.message.reply_text(
        "✅ Config atualizada:\n" + "\n".join(f"`{k}` = `{v}`" for k,v in cfg.items()),
        parse_mode="Markdown"
    )


@owner_only
async def cmd_config_video_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    video_settings.reset(update.effective_user.id)
    await update.message.reply_text("✅ Configurações resetadas.")


@owner_only
async def cmd_video_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await asyncio.to_thread(vc.limpar_tmp)
    if result.get("ok"):
        await update.message.reply_text(f"🗑 {result.get('removidos',0)} arquivo(s) removido(s).")
    else:
        await update.message.reply_text(f"❌ {result.get('error')}")


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END


# ─── Estados compartilhados (teclado inline → texto) ─────────
# Quando o usuário vem de um callback e precisa digitar algo,
# o AGUARDANDO_LINK captura o texto E os callbacks dos botões

_EDICAO_STATES = {
    AGUARDANDO_LINK: [
        CallbackQueryHandler(on_dl_action, pattern=r"^dl:"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receber_link),
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_video_arquivo),
    ],
    AGUARDANDO_FUNDO_DL: [
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, receber_fundo_dl),
        MessageHandler(filters.TEXT, receber_fundo_dl),
    ],
    AGUARDANDO_WATERMARK: [
        MessageHandler(filters.TEXT, receber_watermark),
    ],
    AGUARDANDO_CAPTION: [
        MessageHandler(filters.TEXT, receber_caption),
    ],
    AGUARDANDO_CROP: [
        MessageHandler(filters.TEXT, receber_crop),
    ],
    AGUARDANDO_VIDEO: [
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_video_arquivo),
    ],
}


# ─── Registro ────────────────────────────────────────────────

def register_video_handlers(app):
    # Conversa unificada — download de link OU upload de arquivo + edições
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("download", cmd_download),
            CommandHandler("video",    cmd_video),
        ],
        states=_EDICAO_STATES,
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
    ))

    # Lote separado
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
        per_message=False,
    ))

    app.add_handler(CommandHandler("video_status",       cmd_video_status))
    app.add_handler(CommandHandler("config_video",       cmd_config_video))
    app.add_handler(CommandHandler("config_video_reset", cmd_config_video_reset))
    app.add_handler(CommandHandler("video_limpar",       cmd_video_limpar))
