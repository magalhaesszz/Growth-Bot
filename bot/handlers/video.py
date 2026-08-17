import asyncio
import json
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters,
)

from config import TELEGRAM_OWNER_ID
import video_client as vc
import video_settings

logger = logging.getLogger(__name__)

# Estado da conversa
AGUARDANDO_FUNDO        = 10
AGUARDANDO_VIDEO        = 11
AGUARDANDO_LOTE         = 12
AGUARDANDO_EDITOR       = 13
AGUARDANDO_EDITOR_APPLY = 14
AGUARDANDO_LINK         = 15
AGUARDANDO_WATERMARK    = 16
AGUARDANDO_CAPTION      = 17
AGUARDANDO_CROP         = 18
AGUARDANDO_FUNDO_DL     = 19

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
    return video_settings.get_config(update.effective_user.id)


# ─── /download — link Instagram ou TikTok ────────────────────

@owner_only
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 *Envie o link do vídeo*\n\n"
        "Suportado: Instagram e TikTok\n"
        "Ex: https://www.instagram.com/reel/...\n\n"
        "_Use /cancelar para sair._",
        parse_mode="Markdown"
    )
    return AGUARDANDO_LINK


async def receber_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    url  = msg.text.strip() if msg.text else ""

    if not url.startswith("http"):
        await msg.reply_text("❌ Envie um link válido começando com http(s)://")
        return AGUARDANDO_LINK

    await msg.reply_text("⏳ Baixando vídeo... Aguarde.")

    result = await asyncio.to_thread(vc.download_link, url)

    if not result.get("ok"):
        await msg.reply_text(
            f"❌ Falha no download:\n`{result.get('error', 'Erro desconhecido')}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    job_id   = result["job_id"]
    filename = result["filename"]
    size_mb  = result["size_mb"]

    ctx.user_data["dl_job_id"]   = job_id
    ctx.user_data["dl_filename"] = filename
    ctx.user_data["dl_edits"]    = {}  # edições acumuladas

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Mudar fundo",    callback_data="dl:fundo"),
         InlineKeyboardButton("✂️ Cortar",         callback_data="dl:crop")],
        [InlineKeyboardButton("💧 Marca d'água",   callback_data="dl:watermark"),
         InlineKeyboardButton("📝 Legenda",        callback_data="dl:caption")],
        [InlineKeyboardButton("▶️ Processar tudo", callback_data="dl:processar")],
        [InlineKeyboardButton("❌ Cancelar",       callback_data="dl:cancelar")],
    ])
    await msg.reply_text(
        f"✅ *Vídeo baixado!* ({size_mb} MB)\n"
        f"📄 `{filename}`\n\n"
        f"Escolha as edições ou clique em *Processar tudo*:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_LINK


async def on_download_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.replace("dl:", "")

    job_id   = ctx.user_data.get("dl_job_id")
    filename = ctx.user_data.get("dl_filename", "video.mp4")
    edits    = ctx.user_data.get("dl_edits", {})

    if action == "cancelar":
        ctx.user_data.clear()
        await query.edit_message_text("❌ Download cancelado.")
        return ConversationHandler.END

    if action == "fundo":
        await query.edit_message_text(
            "🎨 *Envie a imagem de fundo*\n\n"
            "PNG ou JPG, ideal 1080x1920px.\n"
            "Ou envie /pular para continuar sem fundo.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_FUNDO_DL

    if action == "watermark":
        ctx.user_data["dl_pending"] = "watermark"
        await query.edit_message_text(
            "💧 *Marca d\'água*\n\n"
            "Digite o texto (ex: @seuusuario):\n"
            "Ou envie /pular para não adicionar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_WATERMARK

    if action == "caption":
        ctx.user_data["dl_pending"] = "caption"
        await query.edit_message_text(
            "📝 *Legenda*\n\n"
            "Digite o texto que aparecerá na parte inferior:\n"
            "Ou envie /pular para não adicionar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CAPTION

    if action == "crop":
        ctx.user_data["dl_pending"] = "crop"
        await query.edit_message_text(
            "✂️ *Cortar vídeo*\n\n"
            "Digite início e fim em segundos (ex: `5-30`):\n"
            "Ou envie /pular para não cortar.",
            parse_mode="Markdown"
        )
        return AGUARDANDO_CROP

    if action == "processar":
        await query.edit_message_text("⏳ Processando vídeo com as edições selecionadas...")

        # Baixar o vídeo do Railway
        video_bytes = await asyncio.to_thread(vc.buscar_video_baixado, job_id)
        if not video_bytes:
            await query.message.reply_text("❌ Erro ao recuperar o vídeo. Tente novamente.")
            return ConversationHandler.END

        # Aplicar edições (marca d'água, legenda, corte)
        watermark = edits.get("watermark", "")
        caption   = edits.get("caption", "")
        crop_s    = edits.get("crop_start", 0.0)
        crop_e    = edits.get("crop_end", 0.0)

        needs_edit = any([watermark, caption, crop_s, crop_e])

        if needs_edit:
            result = await asyncio.to_thread(
                vc.editar_video,
                video_bytes, filename,
                watermark, caption, crop_s, crop_e
            )
        else:
            # Só processamento de fundo (já existente)
            result = await asyncio.to_thread(
                vc.processar_video,
                video_bytes, filename,
                _account_id(update), _cfg(update)
            )

        if result.get("ok"):
            await query.message.reply_video(
                video=result["video_bytes"],
                filename=result["filename"],
                caption=f"✅ *Vídeo pronto!* {result['size_mb']} MB",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(
                f"❌ Erro: `{result.get('error')}`",
                parse_mode="Markdown"
            )

        ctx.user_data.clear()
        return ConversationHandler.END

    return AGUARDANDO_LINK


async def _mostrar_menu_edicao(msg, filename, edits):
    badges = {
        "watermark": "💧✅" if edits.get("watermark") else "💧",
        "caption":   "📝✅" if edits.get("caption")   else "📝",
        "crop":      "✂️✅" if edits.get("crop_start") else "✂️",
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Mudar fundo",              callback_data="dl:fundo"),
         InlineKeyboardButton(f"{badges['crop']} Cortar",    callback_data="dl:crop")],
        [InlineKeyboardButton(f"{badges['watermark']} Marca d\'água", callback_data="dl:watermark"),
         InlineKeyboardButton(f"{badges['caption']} Legenda", callback_data="dl:caption")],
        [InlineKeyboardButton("▶️ Processar tudo", callback_data="dl:processar")],
        [InlineKeyboardButton("❌ Cancelar",       callback_data="dl:cancelar")],
    ])
    await msg.reply_text(
        f"📄 `{filename}`\nEscolha as edições:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def receber_watermark(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    if text and text != "/pular":
        ctx.user_data.setdefault("dl_edits", {})["watermark"] = text
    await update.message.reply_text(
        f"✅ Marca d'água: `{text}`" if text and text != "/pular" else "⏭ Sem marca d'água.",
        parse_mode="Markdown"
    )
    await _mostrar_menu_edicao(
        update.message,
        ctx.user_data.get("dl_filename", "video.mp4"),
        ctx.user_data.get("dl_edits", {})
    )
    return AGUARDANDO_LINK


async def receber_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    if text and text != "/pular":
        ctx.user_data.setdefault("dl_edits", {})["caption"] = text
    await update.message.reply_text(
        f"✅ Legenda: `{text}`" if text and text != "/pular" else "⏭ Sem legenda.",
        parse_mode="Markdown"
    )
    await _mostrar_menu_edicao(
        update.message,
        ctx.user_data.get("dl_filename", "video.mp4"),
        ctx.user_data.get("dl_edits", {})
    )
    return AGUARDANDO_LINK


async def receber_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    if text and text != "/pular" and "-" in text:
        try:
            parts = text.split("-")
            s = float(parts[0])
            e = float(parts[1])
            edits = ctx.user_data.setdefault("dl_edits", {})
            edits["crop_start"] = s
            edits["crop_end"]   = e
            await update.message.reply_text(f"✅ Corte: `{s}s` até `{e}s`", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text("❌ Formato inválido. Use: `5-30`", parse_mode="Markdown")
            return AGUARDANDO_CROP
    else:
        await update.message.reply_text("⏭ Sem corte.")
    await _mostrar_menu_edicao(
        update.message,
        ctx.user_data.get("dl_filename", "video.mp4"),
        ctx.user_data.get("dl_edits", {})
    )
    return AGUARDANDO_LINK



async def receber_fundo_dl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    edits = ctx.user_data.setdefault("dl_edits", {})

    photo = msg.photo or (
        msg.document if msg.document and
        msg.document.mime_type and
        msg.document.mime_type.startswith("image") else None
    )

    if not photo:
        await msg.reply_text("❌ Envie uma imagem PNG ou JPG.")
        return AGUARDANDO_FUNDO_DL

    file_obj    = await (photo[-1] if isinstance(photo, list) else photo).get_file()
    fundo_bytes = bytes(await file_obj.download_as_bytearray())
    filename_fundo = getattr(msg.document, "file_name", "fundo.png") if msg.document else "fundo.png"

    result = await asyncio.to_thread(
        vc.salvar_fundo, fundo_bytes, filename_fundo, _account_id(update)
    )

    if result.get("ok"):
        edits["fundo"] = True
        await msg.reply_text("✅ Fundo salvo! Será aplicado ao processar.")
    else:
        await msg.reply_text(f"❌ Erro ao salvar fundo: {result.get('error')}")

    await _mostrar_menu_edicao(
        msg,
        ctx.user_data.get("dl_filename", "video.mp4"),
        edits
    )
    return AGUARDANDO_LINK


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

    result = await asyncio.to_thread(
        vc.salvar_fundo, bytes(fundo_bytes), filename, _account_id(update)
    )
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

    result = await asyncio.to_thread(
        vc.processar_video,
        bytes(video_bytes),
        filename,
        _account_id(update),
        _cfg(update),
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

    result = await asyncio.to_thread(
        vc.processar_lote, lote, _account_id(update), _cfg(update)
    )

    if not result.get("resultados"):
        await update.message.reply_text(f"❌ Erro: {result.get('error')}")
        return ConversationHandler.END

    for item in result["resultados"]:
        if item["ok"]:
            video_bytes = await asyncio.to_thread(
                vc.download_lote_video, item["job_id"]
            )
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
    status = await asyncio.to_thread(vc.api_status)
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
    cfg = video_settings.get_config(uid)

    if not ctx.args:
        defaults = await asyncio.to_thread(vc.config_default)
        atual = {**(defaults if defaults.get("ok", True) else {}), **cfg}
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

    try:
        cfg = video_settings.set_values(uid, cfg)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    await update.message.reply_text(
        f"✅ Configuração atualizada:\n" +
        "\n".join(f"  `{k}` = `{v}`" for k, v in cfg.items()),
        parse_mode="Markdown"
    )


# ─── /config_video_reset ─────────────────────────────────────

@owner_only
async def cmd_config_video_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    video_settings.reset(update.effective_user.id)
    await update.message.reply_text("✅ Configurações de vídeo resetadas para o padrão.")


# ─── /video_limpar ───────────────────────────────────────────

@owner_only
async def cmd_video_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await asyncio.to_thread(vc.limpar_tmp)
    if result.get("ok"):
        await update.message.reply_text(f"🗑 {result.get('removidos', 0)} arquivo(s) removido(s) do servidor.")
    else:
        await update.message.reply_text(f"❌ Erro: {result.get('error')}")


# ─── /video_editor — preview arrastável ──────────────────────

@owner_only
async def cmd_video_editor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎛 *Editor visual*\n\n"
        "Envie um vídeo .mp4. Depois você poderá mover o vídeo com o mouse "
        "ou com o dedo e ajustar o tamanho antes de processar.",
        parse_mode="Markdown",
    )
    return AGUARDANDO_EDITOR


async def receber_video_editor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media = msg.video or msg.document
    filename = getattr(media, "file_name", None) or "video_editor.mp4"
    if not media or not filename.lower().endswith(".mp4"):
        await msg.reply_text("❌ Envie um vídeo .mp4.")
        return AGUARDANDO_EDITOR

    await msg.reply_text("⏳ Preparando o preview interativo...")
    file_obj = await media.get_file()
    video_bytes = bytes(await file_obj.download_as_bytearray())
    result = await asyncio.to_thread(
        vc.criar_editor_session,
        video_bytes,
        filename,
        _account_id(update),
        _cfg(update),
    )
    if not result.get("ok"):
        await msg.reply_text(f"❌ Não foi possível abrir o editor: {result.get('error')}")
        return ConversationHandler.END

    token = result["token"]
    ctx.user_data["video_editor_source"] = (video_bytes, filename)
    ctx.user_data["video_editor_token"] = token
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖐 Abrir editor visual", url=result["editor_url"])],
        [InlineKeyboardButton(
            "✅ Aplicar posição e processar",
            callback_data=f"video:editor_apply:{token}",
        )],
        [InlineKeyboardButton("❌ Cancelar", callback_data="video:editor_cancel")],
    ])
    await msg.reply_text(
        "✅ Preview pronto. Abra o editor, arraste e redimensione o vídeo, "
        "toque em *Salvar* e depois volte aqui para aplicar.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AGUARDANDO_EDITOR_APPLY


async def aplicar_video_editor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "video:editor_cancel":
        ctx.user_data.pop("video_editor_source", None)
        ctx.user_data.pop("video_editor_token", None)
        await query.edit_message_text("❌ Editor cancelado.")
        return ConversationHandler.END

    token = query.data.rsplit(":", 1)[-1]
    source = ctx.user_data.get("video_editor_source")
    if not source or token != ctx.user_data.get("video_editor_token"):
        await query.edit_message_text("❌ Sessão do editor expirada. Use /video_editor novamente.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Aplicando a posição e processando o vídeo...")
    editor = await asyncio.to_thread(vc.obter_editor_result, token)
    if not editor.get("ok"):
        await query.message.reply_text(f"❌ Editor expirado: {editor.get('error')}")
        return ConversationHandler.END

    editable = {
        key: value
        for key, value in editor["config"].items()
        if key in video_settings.DEFAULTS
    }
    config = video_settings.set_values(update.effective_user.id, editable)
    video_bytes, filename = source
    result = await asyncio.to_thread(
        vc.processar_video,
        video_bytes,
        filename,
        _account_id(update),
        config,
    )
    if result.get("ok"):
        await query.message.reply_video(
            video=result["video_bytes"],
            filename=result["filename"],
            caption=f"✅ Vídeo processado com o layout visual — {result['size_mb']} MB",
        )
    else:
        await query.message.reply_text(f"❌ Falha no processamento: {result.get('error')}")
    ctx.user_data.pop("video_editor_source", None)
    ctx.user_data.pop("video_editor_token", None)
    return ConversationHandler.END


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
        per_message=False,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("video_editor", cmd_video_editor)],
        states={
            AGUARDANDO_EDITOR: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_video_editor),
            ],
            AGUARDANDO_EDITOR_APPLY: [
                CallbackQueryHandler(aplicar_video_editor, pattern=r"^video:editor_"),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
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
        per_message=False,
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
        per_message=False,
    ))

    # Conversa /download
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("download", cmd_download)],
        states={
            AGUARDANDO_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_link),
                CallbackQueryHandler(on_download_action, pattern=r"^dl:"),
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
            AGUARDANDO_FUNDO_DL: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receber_fundo_dl),
                CommandHandler("pular", lambda u, c: receber_fundo_dl(u, c)),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_user=True,
        per_message=False,
    ))

    # Comandos simples
    app.add_handler(CommandHandler("video_status",       cmd_video_status))
    app.add_handler(CommandHandler("config_video",       cmd_config_video))
    app.add_handler(CommandHandler("config_video_reset", cmd_config_video_reset))
    app.add_handler(CommandHandler("video_limpar",       cmd_video_limpar))
