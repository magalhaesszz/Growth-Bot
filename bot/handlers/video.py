import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ConversationHandler,
    ContextTypes, MessageHandler, filters,
)

from config import TELEGRAM_OWNER_ID
from database.videos import VideoDB
import video_client as vc
import video_settings

logger = logging.getLogger(__name__)
vdb = VideoDB()

# ─── Estados ─────────────────────────────────────────────────
AGUARDANDO_FUNDO     = 10
AGUARDANDO_LOTE      = 12
AGUARDANDO_LINK      = 15
AGUARDANDO_WATERMARK = 16
AGUARDANDO_CAPTION   = 17
AGUARDANDO_CROP      = 18
AGUARDANDO_FUNDO_DL  = 19
AGUARDANDO_SPEED     = 20
AGUARDANDO_FLIP      = 21


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from bot.access import has_access
        uid = update.effective_user.id if update.effective_user else 0
        if not has_access(uid):
            if update.message:
                await update.message.reply_text("⛔ Acesso negado.")
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


def _uid(update: Update) -> int:
    return update.effective_user.id


def _cfg(update: Update) -> dict:
    return video_settings.get_config(update.effective_user.id)


def _menu_edicao_kb(edits: dict, video_id: str = "") -> InlineKeyboardMarkup:
    wm  = "\U0001f4a7\u2705" if edits.get("watermark") else "\U0001f4a7"
    cap = "\U0001f4dd\u2705" if edits.get("caption")   else "\U0001f4dd"
    cr  = "\u2702\ufe0f\u2705" if edits.get("crop_start") else "\u2702\ufe0f"
    fd  = "\U0001f3a8\u2705" if edits.get("fundo")    else "\U0001f3a8"
    suffix = f":{video_id}" if video_id else ""
    return InlineKeyboardMarkup([
        # Linha 1: Fundo e Cortar
        [InlineKeyboardButton(f"{fd} Fundo",          callback_data=f"dl:fundo{suffix}"),
         InlineKeyboardButton(f"{cr} Cortar",         callback_data=f"dl:crop{suffix}")],
        # Linha 2: Marca dagua e Legenda
        [InlineKeyboardButton(f"{wm} Marca d'agua",   callback_data=f"dl:watermark{suffix}"),
         InlineKeyboardButton(f"{cap} Legenda",       callback_data=f"dl:caption{suffix}")],
        # Linha 3: Velocidade e Flip
        [InlineKeyboardButton("\u23e9 Velocidade",    callback_data=f"dl:speed{suffix}"),
         InlineKeyboardButton("\U0001f503 Espelhar",  callback_data=f"dl:flip{suffix}")],
        # Linha 4: Processar
        [InlineKeyboardButton("\u25b6\ufe0f Processar", callback_data=f"dl:processar{suffix}")],
        # Linha 5: Biblioteca e Cancelar
        [InlineKeyboardButton("\U0001f5c2 Biblioteca", callback_data="dl:biblioteca"),
         InlineKeyboardButton("\u274c Cancelar",      callback_data="dl:cancelar")],
    ])


# ─── /download ───────────────────────────────────────────────

@owner_only
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "🔗 *Envie o link ou o vídeo*\n\n"
        "• Link do Instagram ou TikTok\n"
        "• Ou envie um arquivo .mp4 direto\n\n"
        "_/cancelar para sair_",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LINK


async def receber_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Envie um link válido ou um arquivo .mp4.")
        return AGUARDANDO_LINK

    status_msg = await update.message.reply_text("⏳ Baixando vídeo...")
    result = await asyncio.to_thread(vc.download_link, url)

    if not result.get("ok"):
        await status_msg.edit_text(
            f"❌ Falha no download:\n`{result.get('error','Erro desconhecido')}`",
            parse_mode="Markdown")
        return ConversationHandler.END

    video_bytes = result["video_bytes"]
    filename    = result["filename"]
    size_mb     = result["size_mb"]

    # Salvar no banco (opcional — não bloqueia se falhar)
    video_id = ""
    try:
        await status_msg.edit_text("⏳ Salvando na biblioteca...")
        storage_path = await asyncio.to_thread(
            vdb.upload_video, video_bytes, filename, _uid(update))
        if storage_path:
            record   = await asyncio.to_thread(
                vdb.save_video, _uid(update), filename,
                storage_path, url, size_mb)
            video_id = record.get("id", "")
            ctx.user_data["dl_storage_path"] = storage_path
    except Exception as e:
        logger.warning(f"Erro ao salvar no banco (nao critico): {e}")

    # Guardar bytes em memória como fallback
    ctx.user_data["dl_video_id"]    = video_id
    ctx.user_data["dl_video_bytes"] = video_bytes
    ctx.user_data["dl_filename"]    = filename
    ctx.user_data["dl_edits"]       = {}

    saved = "Salvo na biblioteca!" if video_id else "Pronto!"
    await status_msg.edit_text(
        f"\u2705 *{saved}* ({size_mb} MB)\n"
        f"\U0001f3ac `{filename}`\n\n"
        f"Agora escolha as edicoes:",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "\U0001f3a8 Fundo  \u2702\ufe0f Cortar  \U0001f4a7 Marca  \U0001f4dd Legenda  \u25b6\ufe0f Processar",
        reply_markup=_menu_edicao_kb({}, video_id))
    return AGUARDANDO_LINK


async def receber_mp4(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc, vid = msg.document, msg.video
    file_ref, filename = None, "video.mp4"

    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref, filename = doc, doc.file_name
    elif vid:
        file_ref, filename = vid, f"video_{vid.file_unique_id}.mp4"
    else:
        await msg.reply_text("❌ Envie um link ou um arquivo .mp4.")
        return AGUARDANDO_LINK

    status_msg = await msg.reply_text("⏳ Recebendo vídeo...")
    file_obj    = await file_ref.get_file()
    video_bytes = bytes(await file_obj.download_as_bytearray())
    size_mb     = round(len(video_bytes) / 1048576, 2)

    await status_msg.edit_text("⏳ Salvando no banco...")
    storage_path = await asyncio.to_thread(
        vdb.upload_video, video_bytes, filename, _uid(update))
    if not storage_path:
        await status_msg.edit_text("❌ Erro ao salvar no banco.")
        return ConversationHandler.END

    record = await asyncio.to_thread(
        vdb.save_video, _uid(update), filename, storage_path, "", size_mb)

    # Salvar no banco (opcional)
    video_id = ""
    try:
        storage_path = await asyncio.to_thread(
            vdb.upload_video, video_bytes, filename, _uid(update))
        if storage_path:
            record   = await asyncio.to_thread(
                vdb.save_video, _uid(update), filename, storage_path, "", size_mb)
            video_id = record.get("id", "")
            ctx.user_data["dl_storage_path"] = storage_path
    except Exception as e:
        logger.warning(f"Erro ao salvar mp4 no banco: {e}")

    ctx.user_data["dl_video_id"]    = video_id
    ctx.user_data["dl_video_bytes"] = video_bytes
    ctx.user_data["dl_filename"]    = filename
    ctx.user_data["dl_edits"]       = {}

    saved = "Salvo na biblioteca!" if video_id else "Pronto!"
    await status_msg.edit_text(
        f"\u2705 *{saved}* ({size_mb} MB)\n\U0001f3ac `{filename}`",
        parse_mode="Markdown"
    )
    await msg.reply_text(
        "\U0001f3a8 Fundo  \u2702\ufe0f Cortar  \U0001f4a7 Marca  \U0001f4dd Legenda  \u25b6\ufe0f Processar",
        reply_markup=_menu_edicao_kb({}, video_id))
    return AGUARDANDO_LINK


# ─── /biblioteca ─────────────────────────────────────────────

@owner_only
async def cmd_biblioteca(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    videos = await asyncio.to_thread(vdb.list_videos, _uid(update))
    if not videos:
        await update.message.reply_text(
            "📂 *Biblioteca vazia*\n\nUse /download para adicionar vídeos.",
            parse_mode="Markdown")
        return ConversationHandler.END

    rows = []
    for v in videos[:10]:
        label = f"{'✅' if v['status']=='processed' else '📥'} {v['filename'][:30]} ({v['size_mb']} MB)"
        rows.append([InlineKeyboardButton(label, callback_data=f"lib:select:{v['id']}")])
    rows.append([InlineKeyboardButton("❌ Fechar", callback_data="lib:fechar")])

    await update.message.reply_text(
        f"📂 *Biblioteca — {len(videos)} vídeo(s)*\n\nToque para editar ou processar:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )
    return AGUARDANDO_LINK


async def on_biblioteca(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "lib:fechar":
        await query.edit_message_text("📂 Biblioteca fechada.")
        return ConversationHandler.END

    if data.startswith("lib:select:"):
        vid_id = data.replace("lib:select:", "")
        record = await asyncio.to_thread(vdb.get_video, vid_id)
        if not record:
            await query.edit_message_text("❌ Vídeo não encontrado.")
            return ConversationHandler.END

        ctx.user_data["dl_video_id"]     = vid_id
        ctx.user_data["dl_storage_path"] = record["storage_path"]
        ctx.user_data["dl_filename"]     = record["filename"]
        ctx.user_data["dl_edits"]        = {}

        await query.edit_message_text(
            f"🎬 *{record['filename']}*\n"
            f"Tamanho: {record['size_mb']} MB\n"
            f"Status: {record['status']}\n\n"
            f"Escolha as edições:",
            reply_markup=_menu_edicao_kb({}, vid_id),
            parse_mode="Markdown"
        )
        return AGUARDANDO_LINK

    if data.startswith("lib:delete:"):
        vid_id = data.replace("lib:delete:", "")
        record = await asyncio.to_thread(vdb.get_video, vid_id)
        if record:
            await asyncio.to_thread(vdb.delete_video, record["storage_path"])
            await asyncio.to_thread(vdb.delete_record, vid_id)
        await query.edit_message_text("🗑 Vídeo removido da biblioteca.")
        return ConversationHandler.END

    return AGUARDANDO_LINK


# ─── Callbacks dos botões de edição ──────────────────────────

async def on_dl_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    raw    = query.data.replace("dl:", "")
    # Separar action de video_id (formato: action:video_id)
    parts  = raw.split(":", 1)
    action = parts[0]
    if len(parts) > 1 and parts[1]:
        ctx.user_data["dl_video_id"] = parts[1]
    edits = ctx.user_data.setdefault("dl_edits", {})

    if action == "cancelar":
        ctx.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END

    if action == "biblioteca":
        videos = await asyncio.to_thread(vdb.list_videos, _uid(update))
        if not videos:
            await query.edit_message_text("📂 Biblioteca vazia. Use /download para adicionar vídeos.")
            return AGUARDANDO_LINK
        rows = []
        for v in videos[:10]:
            label = f"{'✅' if v['status']=='processed' else '📥'} {v['filename'][:25]} ({v['size_mb']} MB)"
            rows.append([InlineKeyboardButton(label, callback_data=f"lib:select:{v['id']}")])
        rows.append([InlineKeyboardButton("❌ Fechar", callback_data="lib:fechar")])
        await query.edit_message_text(
            f"📂 *{len(videos)} vídeo(s) na biblioteca:*",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown")
        return AGUARDANDO_LINK

    if action == "fundo":
        await query.edit_message_text(
            "🎨 *Envie a imagem de fundo*\n\nPNG ou JPG, ideal 1080x1920px.\n"
            "Ou envie /pular para continuar sem fundo.",
            parse_mode="Markdown")
        return AGUARDANDO_FUNDO_DL

    if action == "watermark":
        await query.edit_message_text(
            "💧 *Marca d'água*\n\nDigite o texto (ex: @usuario):\n/pular para não adicionar.",
            parse_mode="Markdown")
        return AGUARDANDO_WATERMARK

    if action == "caption":
        await query.edit_message_text(
            "📝 *Legenda*\n\nDigite o texto para a parte inferior:\n/pular para não adicionar.",
            parse_mode="Markdown")
        return AGUARDANDO_CAPTION

    if action == "crop":
        await query.edit_message_text(
            "✂️ *Cortar*\n\nDigite início-fim em segundos (ex: `5-30`):\n/pular para não cortar.",
            parse_mode="Markdown")
        return AGUARDANDO_CROP

    if action == "processar":
        await query.edit_message_text("⏳ Processando vídeo...")
        result = await _processar(update, ctx)
        if result.get("ok"):
            # Salvar vídeo processado na biblioteca
            vid_bytes = result["video_bytes"]
            fname     = result["filename"]
            sp = await asyncio.to_thread(vdb.upload_video, vid_bytes, fname, _uid(update))
            if sp:
                rec = await asyncio.to_thread(
                    vdb.save_video, _uid(update), fname, sp, "", result["size_mb"])
                rec_id = rec.get("id","")
                await asyncio.to_thread(vdb.update_status, rec_id, "processed")

            await query.message.reply_video(
                video=vid_bytes,
                filename=fname,
                caption=f"✅ *Pronto!* {result['size_mb']} MB — salvo na biblioteca.",
                parse_mode="Markdown"
            )
            # Marcar original como processado
            orig_id = ctx.user_data.get("dl_video_id")
            if orig_id:
                await asyncio.to_thread(vdb.update_status, orig_id, "processed")
        else:
            await query.message.reply_text(
                f"❌ Erro: `{result.get('error')}`", parse_mode="Markdown")
        ctx.user_data.clear()
        return ConversationHandler.END

    return AGUARDANDO_LINK


async def _processar(update, ctx) -> dict:
    vid_id   = ctx.user_data.get("dl_video_id")
    sp       = ctx.user_data.get("dl_storage_path")
    fname    = ctx.user_data.get("dl_filename", "video.mp4")
    edits    = ctx.user_data.get("dl_edits", {})

    # Tentar bytes em memória primeiro (mais rápido, sem roundtrip ao banco)
    vid_bytes = ctx.user_data.get("dl_video_bytes")

    if not vid_bytes and sp:
        vid_bytes = await asyncio.to_thread(vdb.download_video, sp)

    if not vid_bytes and not sp:
        return {"ok": False, "error": "Nenhum video disponivel. Use /download novamente."}
    if not vid_bytes:
        return {"ok": False, "error": "Não foi possível recuperar o vídeo do banco."}

    wm     = edits.get("watermark", "")
    cap    = edits.get("caption", "")
    crop_s = edits.get("crop_start", 0.0)
    crop_e = edits.get("crop_end", 0.0)
    speed  = edits.get("speed", 0.0)
    flip   = edits.get("flip", False)

    if wm or cap or crop_s or crop_e or speed or flip:
        result = await asyncio.to_thread(
            vc.editar_video, vid_bytes, fname, wm, cap, crop_s, crop_e, speed, flip)
        if not result.get("ok"):
            return result
        vid_bytes = result["video_bytes"]
        fname     = result["filename"]

    # Verificar se há fundo salvo no banco — se sim, enviar para a API
    fundo_bytes = await asyncio.to_thread(vdb.get_fundo, _uid(update))
    if fundo_bytes:
        await asyncio.to_thread(vc.salvar_fundo, fundo_bytes, "fundo.png", str(_uid(update)))
    return await asyncio.to_thread(
        vc.processar_video, vid_bytes, fname, str(_uid(update)), _cfg(update))


# ─── Inputs de edição ────────────────────────────────────────

async def receber_fundo_dl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    text  = (msg.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    vid_id = ctx.user_data.get("dl_video_id","")

    if text == "/pular":
        await msg.reply_text("⏭ Sem fundo.",
            reply_markup=_menu_edicao_kb(edits, vid_id))
        return AGUARDANDO_LINK

    photo = msg.photo or (
        msg.document if msg.document and
        msg.document.mime_type and
        msg.document.mime_type.startswith("image") else None)
    if not photo:
        await msg.reply_text("❌ Envie uma imagem PNG/JPG ou /pular.")
        return AGUARDANDO_FUNDO_DL

    file_obj = await (photo[-1] if isinstance(photo,list) else photo).get_file()
    fb = bytes(await file_obj.download_as_bytearray())
    fn = getattr(msg.document,"file_name","fundo.png") if msg.document else "fundo.png"

    # Salvar no Supabase Storage (substitui o anterior)
    sp = await asyncio.to_thread(vdb.save_fundo, _uid(update), fb, fn)
    if sp:
        edits["fundo"] = True
        # Também enviar para a Video API (para o processamento de fundo)
        await asyncio.to_thread(vc.salvar_fundo, fb, fn, str(_uid(update)))
        await msg.reply_text(
            "✅ *Fundo salvo na biblioteca!*\nSerá aplicado ao processar.",
            reply_markup=_menu_edicao_kb(edits, vid_id),
            parse_mode="Markdown")
    else:
        await msg.reply_text(
            "❌ Erro ao salvar fundo. Tente novamente.",
            reply_markup=_menu_edicao_kb(edits, vid_id))
    return AGUARDANDO_LINK


async def receber_watermark(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    vid_id = ctx.user_data.get("dl_video_id","")
    if text and text != "/pular":
        edits["watermark"] = text
    await update.message.reply_text(
        f"✅ Marca d'água: `{text}`" if text and text!="/pular" else "⏭ Sem marca d'água.",
        reply_markup=_menu_edicao_kb(edits, vid_id), parse_mode="Markdown")
    return AGUARDANDO_LINK


async def receber_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    vid_id = ctx.user_data.get("dl_video_id","")
    if text and text != "/pular":
        edits["caption"] = text
    await update.message.reply_text(
        f"✅ Legenda: `{text}`" if text and text!="/pular" else "⏭ Sem legenda.",
        reply_markup=_menu_edicao_kb(edits, vid_id), parse_mode="Markdown")
    return AGUARDANDO_LINK



async def receber_speed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    vid_id = ctx.user_data.get("dl_video_id","")
    if text and text != "/pular":
        try:
            speed = float(text)
            if 0.25 <= speed <= 4.0:
                edits["speed"] = speed
                await update.message.reply_text(
                    f"\u2705 Velocidade: `{speed}x`",
                    reply_markup=_menu_edicao_kb(edits, vid_id), parse_mode="Markdown")
            else:
                await update.message.reply_text("\u274c Use valor entre 0.25 e 4.0")
                return AGUARDANDO_SPEED
        except ValueError:
            await update.message.reply_text("\u274c Digite um numero. Ex: `1.5`", parse_mode="Markdown")
            return AGUARDANDO_SPEED
    else:
        await update.message.reply_text("\u23ed Velocidade normal.",
            reply_markup=_menu_edicao_kb(edits, vid_id))
    return AGUARDANDO_LINK

async def receber_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    vid_id = ctx.user_data.get("dl_video_id","")
    if text and text != "/pular" and "-" in text:
        try:
            s, e = text.split("-", 1)
            edits["crop_start"] = float(s)
            edits["crop_end"]   = float(e)
            await update.message.reply_text(
                f"✅ Corte: `{s}s` até `{e}s`",
                reply_markup=_menu_edicao_kb(edits, vid_id), parse_mode="Markdown")
            return AGUARDANDO_LINK
        except Exception:
            await update.message.reply_text("❌ Formato inválido. Use: `5-30`", parse_mode="Markdown")
            return AGUARDANDO_CROP
    await update.message.reply_text("⏭ Sem corte.",
        reply_markup=_menu_edicao_kb(edits, vid_id))
    return AGUARDANDO_LINK


# ─── /video_lote ─────────────────────────────────────────────


@owner_only
async def cmd_fundo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mostra o fundo atual ou pede para enviar um novo."""
    info = await asyncio.to_thread(vdb.get_fundo_info, _uid(update))
    if info:
        fundo_bytes = await asyncio.to_thread(vdb.get_fundo, _uid(update))
        if fundo_bytes:
            await update.message.reply_photo(
                photo=fundo_bytes,
                caption=(
                    f"🎨 *Fundo atual:* `{info['filename']}`\n\n"
                    f"Para trocar, envie uma nova imagem abaixo:"
                ),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "🎨 Nenhum fundo encontrado no banco.\nEnvie uma imagem PNG/JPG 1080x1920px:")
    else:
        await update.message.reply_text(
            "🎨 *Cadastrar fundo de vídeo*\n\n"
            "Envie uma imagem PNG/JPG (ideal 1080x1920px):",
            parse_mode="Markdown"
        )
    return AGUARDANDO_FUNDO


async def receber_fundo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    photo = msg.photo or (
        msg.document if msg.document and
        msg.document.mime_type and
        msg.document.mime_type.startswith("image") else None)
    if not photo:
        await msg.reply_text("❌ Envie uma imagem PNG ou JPG.")
        return AGUARDANDO_FUNDO

    file_obj = await (photo[-1] if isinstance(photo,list) else photo).get_file()
    fb = bytes(await file_obj.download_as_bytearray())
    fn = getattr(msg.document,"file_name","fundo.png") if msg.document else "fundo.png"

    sp = await asyncio.to_thread(vdb.save_fundo, _uid(update), fb, fn)
    if sp:
        await asyncio.to_thread(vc.salvar_fundo, fb, fn, str(_uid(update)))
        await msg.reply_text(
            "✅ *Fundo salvo e atualizado no banco!*\n"
            "Será aplicado automaticamente em todos os próximos processamentos.",
            parse_mode="Markdown"
        )
    else:
        await msg.reply_text("❌ Erro ao salvar fundo. Tente novamente.")
    return ConversationHandler.END


@owner_only
async def cmd_video_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["lote_videos"] = []
    await update.message.reply_text(
        "📦 *Modo lote* — Envie os vídeos .mp4 (máx 10).\n/processar_lote para processar.",
        parse_mode="Markdown")
    return AGUARDANDO_LOTE


async def coletar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc, vid = msg.document, msg.video
    lote = ctx.user_data.get("lote_videos", [])
    if len(lote) >= 10:
        await msg.reply_text("⚠️ Máximo de 10. Use /processar_lote.")
        return AGUARDANDO_LOTE
    file_ref, filename = None, "video.mp4"
    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        file_ref, filename = doc, doc.file_name
    elif vid:
        file_ref, filename = vid, f"video_{vid.file_unique_id}.mp4"
    if not file_ref:
        await msg.reply_text("❌ Apenas .mp4 ou /processar_lote.")
        return AGUARDANDO_LOTE
    vb = bytes(await (await file_ref.get_file()).download_as_bytearray())
    lote.append((vb, filename))
    ctx.user_data["lote_videos"] = lote
    await msg.reply_text(f"✅ {len(lote)}/10 — *{filename}*", parse_mode="Markdown")
    return AGUARDANDO_LOTE


async def executar_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lote = ctx.user_data.get("lote_videos", [])
    if not lote:
        await update.message.reply_text("Nenhum vídeo na fila.")
        return ConversationHandler.END
    await update.message.reply_text(f"⏳ Processando {len(lote)} vídeo(s)...")
    result = await asyncio.to_thread(
        vc.processar_lote, lote, str(_uid(update)), _cfg(update))
    for item in (result.get("resultados") or []):
        if item["ok"]:
            vb = await asyncio.to_thread(vc.download_lote_video, item["job_id"])
            if vb:
                await update.message.reply_video(video=vb,
                    filename=f"editado_{item['arquivo']}",
                    caption=f"✅ {item['arquivo']} — {item['size_mb']} MB",
                    parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ {item['arquivo']}: {item['error']}")
    ctx.user_data.pop("lote_videos", None)
    return ConversationHandler.END


# ─── Comandos simples ─────────────────────────────────────────

@owner_only
async def cmd_video_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = await asyncio.to_thread(vc.api_status)
    if status.get("ok"):
        await update.message.reply_text(
            f"📡 *Video API*\nFFmpeg: {'✅' if status.get('ffmpeg') else '❌'}\n"
            f"Disco /tmp: {status.get('disco_tmp_livre_mb','?')} MB",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ API offline: {status.get('error')}")


@owner_only
async def cmd_config_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = video_settings.get_config(uid)
    if not ctx.args:
        await update.message.reply_text(
            f"⚙️ *Config:*\nLargura: `{cfg.get('video_width',800)}px` | "
            f"CRF: `{cfg.get('output_crf',18)}` | FPS: `{cfg.get('output_fps',30)}`\n"
            f"Uso: `/config_video chave=valor`", parse_mode="Markdown")
        return
    for arg in ctx.args:
        if "=" not in arg: continue
        k, v = arg.split("=", 1)
        if v.lower() in ("true","false"): cfg[k] = v.lower()=="true"
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
    await update.message.reply_text("✅ Config atualizada.", parse_mode="Markdown")


@owner_only
async def cmd_config_video_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    video_settings.reset(update.effective_user.id)
    await update.message.reply_text("✅ Config resetada.")


@owner_only
async def cmd_video_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await asyncio.to_thread(vc.limpar_tmp)
    await update.message.reply_text(
        f"🗑 {result.get('removidos',0)} arquivo(s) removido(s)." if result.get("ok")
        else f"❌ {result.get('error')}")


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END


# ─── Registro ────────────────────────────────────────────────

def register_video_handlers(app):
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("download",   cmd_download),
            CommandHandler("biblioteca", cmd_biblioteca),
        ],
        states={
            AGUARDANDO_LINK: [
                CallbackQueryHandler(on_dl_action,   pattern=r"^dl:"),
                CallbackQueryHandler(on_biblioteca,  pattern=r"^lib:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_link),
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_mp4),
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
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv)

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("video_lote", cmd_video_lote)],
        states={AGUARDANDO_LOTE: [
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, coletar_lote),
            CommandHandler("processar_lote", executar_lote),
        ]},
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True, per_message=False,
    ))

    # Conversa /fundo — cadastrar/trocar fundo
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("fundo", cmd_fundo)],
        states={AGUARDANDO_FUNDO: [
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, receber_fundo_cmd),
        ]},
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True, per_message=False,
    ))

    app.add_handler(CommandHandler("video_status",       cmd_video_status))
    app.add_handler(CommandHandler("config_video",       cmd_config_video))
    app.add_handler(CommandHandler("config_video_reset", cmd_config_video_reset))
    app.add_handler(CommandHandler("video_limpar",       cmd_video_limpar))
