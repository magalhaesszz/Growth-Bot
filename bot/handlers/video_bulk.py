import asyncio
import json
import logging
import os
import re

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ConversationHandler, ContextTypes,
    MessageHandler, filters,
)

from bot.handlers import video as legacy
import video_client as vc
import video_settings

logger = logging.getLogger(__name__)

AGUARDANDO_LINK = legacy.AGUARDANDO_LINK
AGUARDANDO_FUNDO_DL = legacy.AGUARDANDO_FUNDO_DL
AGUARDANDO_WATERMARK = legacy.AGUARDANDO_WATERMARK
AGUARDANDO_CAPTION = legacy.AGUARDANDO_CAPTION
AGUARDANDO_CROP = legacy.AGUARDANDO_CROP
AGUARDANDO_SPEED = legacy.AGUARDANDO_SPEED

MAX_BATCH = 10
URL_RE = re.compile(r"https?://[^\s<>()]+", re.I)


def _uid(update: Update) -> int:
    return update.effective_user.id


def _urls(text: str) -> list[str]:
    out, seen = [], set()
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}'\"")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _batch(ctx: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    items = ctx.user_data.get("dl_videos")
    if isinstance(items, list):
        return items

    # Compatibilidade com seleção antiga da biblioteca, que preenche dl_*.
    filename = ctx.user_data.get("dl_filename")
    storage_path = ctx.user_data.get("dl_storage_path")
    video_bytes = ctx.user_data.get("dl_video_bytes")
    if filename and (storage_path or video_bytes):
        items = [{
            "video_id": ctx.user_data.get("dl_video_id", ""),
            "filename": filename,
            "storage_path": storage_path or "",
            "size_mb": round(len(video_bytes) / 1048576, 2) if video_bytes else 0.0,
            "video_bytes": bytes(video_bytes) if video_bytes else None,
        }]
        ctx.user_data["dl_videos"] = items
        return items
    return []


def _menu(ctx: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    edits = ctx.user_data.setdefault("dl_edits", {})
    n = len(_batch(ctx))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Fundo", callback_data="dl:fundo"),
         InlineKeyboardButton("✂️ Corte de tempo" + (" ✅" if edits.get("crop_start") else ""), callback_data="dl:crop")],
        [InlineKeyboardButton("💧 Marca" + (" ✅" if edits.get("watermark") else ""), callback_data="dl:watermark"),
         InlineKeyboardButton("📝 Legenda" + (" ✅" if edits.get("caption") else ""), callback_data="dl:caption")],
        [InlineKeyboardButton("⏩ Velocidade", callback_data="dl:speed"),
         InlineKeyboardButton("🔄 Espelhar" + (" ✅" if edits.get("flip") else ""), callback_data="dl:flip")],
        [InlineKeyboardButton(f"🖼️ Editor visual ({n})", callback_data="dl:editor"),
         InlineKeyboardButton(f"▶️ Processar todos ({n})", callback_data="dl:processar")],
        [InlineKeyboardButton("➕ Mais links", callback_data="dl:addlinks"),
         InlineKeyboardButton("🗂 Biblioteca", callback_data="dl:biblioteca")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="dl:cancelar")],
    ])


async def _load(item: dict) -> bytes | None:
    if item.get("video_bytes"):
        return bytes(item["video_bytes"])
    if item.get("storage_path"):
        return await asyncio.to_thread(legacy.vdb.download_video, item["storage_path"])
    return None


async def _persist_input(update: Update, result: dict, source_url: str = "") -> dict:
    data, filename, size_mb = result["video_bytes"], result["filename"], result["size_mb"]
    storage_path = ""
    video_id = ""
    try:
        storage_path = await asyncio.to_thread(legacy.vdb.upload_video, data, filename, _uid(update))
        if storage_path:
            record = await asyncio.to_thread(
                legacy.vdb.save_video, _uid(update), filename, storage_path, source_url, size_mb
            )
            video_id = record.get("id", "") if record else ""
    except Exception as exc:
        logger.warning("Falha ao salvar %s: %s", filename, type(exc).__name__)
    return {
        "video_id": video_id,
        "filename": filename,
        "storage_path": storage_path,
        "size_mb": size_mb,
        "video_bytes": None if storage_path else data,
    }


async def _sync_fundo(update: Update) -> dict:
    try:
        data = await asyncio.to_thread(legacy.vdb.get_fundo, _uid(update))
    except Exception:
        data = None
    if not data:
        return {"ok": False, "error": "Cadastre um fundo antes de abrir o editor ou processar."}
    return await asyncio.to_thread(vc.salvar_fundo, data, "fundo.png", str(_uid(update)))


def _editor_session(videos: list[tuple[bytes, str]], account_id: str, cfg: dict) -> dict:
    try:
        vc._check_configured()
        vc._validate_batch_size(videos)
        files = [("videos", (name, data, "video/mp4")) for data, name in videos]
        with httpx.Client(timeout=vc.TIMEOUT) as client:
            response = client.post(
                f"{vc.VIDEO_API_URL}/api/v1/editor/batch/session",
                headers=vc.HEADERS,
                files=files,
                data={"account_id": account_id, "config_json": json.dumps(cfg)},
            )
        return response.json() if response.is_success else {"ok": False, "error": vc._response_error(response)}
    except Exception as exc:
        return vc._error(exc)


def _editor_result(token: str) -> dict:
    try:
        vc._check_configured()
        with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
            response = client.get(
                f"{vc.VIDEO_API_URL}/api/v1/editor/batch/{token}/result",
                headers=vc.HEADERS,
            )
        return response.json() if response.is_success else {"ok": False, "error": vc._response_error(response)}
    except Exception as exc:
        return vc._error(exc)


async def _send_file(message, path: str, filename: str, size_mb: float) -> bool:
    try:
        with open(path, "rb") as file:
            await message.reply_video(video=file, filename=filename, caption=f"✅ Pronto! {size_mb} MB")
        return True
    except Exception:
        try:
            with open(path, "rb") as file:
                await message.reply_document(document=file, filename=filename, caption=f"✅ Pronto! {size_mb} MB")
            return True
        except Exception as exc:
            logger.error("Telegram recusou %s: %s", filename, type(exc).__name__)
            return False


async def _save_output(update: Update, item: dict, path: str, filename: str, size_mb: float):
    try:
        with open(path, "rb") as file:
            data = file.read()
        storage_path = await asyncio.to_thread(legacy.vdb.upload_video, data, filename, _uid(update))
        if storage_path:
            record = await asyncio.to_thread(legacy.vdb.save_video, _uid(update), filename, storage_path, "", size_mb)
            if record and record.get("id"):
                await asyncio.to_thread(legacy.vdb.update_status, record["id"], "processed")
        if item.get("video_id"):
            await asyncio.to_thread(legacy.vdb.update_status, item["video_id"], "processed")
    except Exception as exc:
        logger.warning("Resultado não salvo na biblioteca (%s): %s", filename, type(exc).__name__)


async def _process_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE, message, cfg: dict, editor_items=None):
    items = _batch(ctx)
    if not items:
        await message.reply_text("❌ Nenhum vídeo carregado.")
        return AGUARDANDO_LINK
    sync = await _sync_fundo(update)
    if not sync.get("ok"):
        await message.reply_text(f"❌ {sync.get('error')}")
        return AGUARDANDO_LINK

    edits = ctx.user_data.get("dl_edits", {})
    visual = {int(x.get("index", i)): x for i, x in enumerate(editor_items or [])}
    ok_count = 0
    for index, item in enumerate(items):
        await message.reply_text(f"⏳ {index + 1}/{len(items)} — {item['filename']}")
        data = await _load(item)
        if not data:
            await message.reply_text(f"❌ {item['filename']}: arquivo indisponível.")
            continue
        filename = item["filename"]

        if any(edits.get(k) for k in ("watermark", "caption", "crop_start", "crop_end", "speed", "flip")):
            edited = await asyncio.to_thread(
                vc.editar_video, data, filename,
                edits.get("watermark", ""), edits.get("caption", ""),
                edits.get("crop_start", 0.0), edits.get("crop_end", 0.0),
                edits.get("speed", 0.0), edits.get("flip", False),
            )
            data = None
            if not edited.get("ok"):
                await message.reply_text(f"❌ {filename}: {edited.get('error')}")
                continue
            data, filename = edited["video_bytes"], edited["filename"]

        process_cfg = dict(cfg)
        visual_item = visual.get(index) or {}
        for key in ("video_width", "position_x", "position_y"):
            if key in visual_item:
                process_cfg[key] = visual_item[key]
        crop = dict(visual_item.get("manual_crop") or {})
        if crop:
            if edits.get("flip") and visual_item.get("source_width"):
                crop["x"] = max(0, int(visual_item["source_width"]) - int(crop["x"]) - int(crop["w"]))
            process_cfg["manual_crop"] = crop

        result = await asyncio.to_thread(
            vc.processar_video_arquivo, data, filename, str(_uid(update)), process_cfg
        )
        data = None
        if not result.get("ok"):
            await message.reply_text(f"❌ {item['filename']}: {result.get('error')}")
            continue
        path = result["video_path"]
        try:
            await _save_output(update, item, path, result["filename"], result["size_mb"])
            if await _send_file(message, path, result["filename"], result["size_mb"]):
                ok_count += 1
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    await message.reply_text(f"✅ Lote concluído: {ok_count}/{len(items)} vídeo(s) enviados.")
    ctx.user_data.clear()
    return ConversationHandler.END


@legacy.owner_only
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["dl_videos"] = []
    ctx.user_data["dl_edits"] = {}
    await update.message.reply_text(
        "🔗 *Envie um ou vários links*\n\n"
        "Cole até 10 links do Instagram/TikTok, um por linha. "
        "Você também pode mandar mais links em seguida ou enviar .mp4.\n\n"
        "Todos os vídeos baixados entrarão no mesmo editor visual.",
        parse_mode="Markdown",
    )
    return AGUARDANDO_LINK


async def receber_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    urls = _urls(update.message.text or "")
    if not urls:
        await update.message.reply_text("❌ Não encontrei links válidos.")
        return AGUARDANDO_LINK
    items = _batch(ctx)
    space = MAX_BATCH - len(items)
    if space <= 0:
        await update.message.reply_text("⚠️ O lote já tem 10 vídeos.", reply_markup=_menu(ctx))
        return AGUARDANDO_LINK
    urls = urls[:space]
    status = await update.message.reply_text(f"⏳ Baixando {len(urls)} link(s)...")
    sem = asyncio.Semaphore(3)

    async def one(url):
        async with sem:
            return url, await asyncio.to_thread(vc.download_link, url)

    results = await asyncio.gather(*(one(url) for url in urls))
    total = sum(float(x.get("size_mb") or 0) for x in items)
    failed = 0
    for url, result in results:
        if not result.get("ok"):
            failed += 1
            continue
        size = float(result.get("size_mb") or 0)
        if total + size > vc.VIDEO_MAX_BATCH_MB:
            failed += 1
            continue
        items.append(await _persist_input(update, result, url))
        total += size
    ctx.user_data["dl_videos"] = items
    if not items:
        await status.edit_text("❌ Nenhum vídeo foi baixado. Verifique os links.")
        return AGUARDANDO_LINK
    await status.edit_text(
        f"✅ {len(items)} vídeo(s) no lote — {round(total, 2)} MB."
        + (f"\n⚠️ {failed} link(s) falharam/foram ignorados." if failed else "")
    )
    await update.message.reply_text("🎬 Lote pronto. Envie mais links ou edite todos juntos.", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def receber_mp4(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc, vid = msg.document, msg.video
    ref = None
    filename = "video.mp4"
    if doc and doc.file_name and doc.file_name.lower().endswith(".mp4"):
        ref, filename = doc, doc.file_name
    elif vid:
        ref, filename = vid, f"video_{vid.file_unique_id}.mp4"
    if not ref:
        await msg.reply_text("❌ Envie um link ou .mp4.")
        return AGUARDANDO_LINK
    items = _batch(ctx)
    if len(items) >= MAX_BATCH:
        await msg.reply_text("⚠️ Máximo de 10 vídeos.")
        return AGUARDANDO_LINK
    data = bytes(await (await ref.get_file()).download_as_bytearray())
    try:
        vc._validate_video_size(data, filename)
    except ValueError as exc:
        await msg.reply_text(f"❌ {exc}")
        return AGUARDANDO_LINK
    size = round(len(data) / 1048576, 2)
    if sum(float(x.get("size_mb") or 0) for x in items) + size > vc.VIDEO_MAX_BATCH_MB:
        await msg.reply_text(f"❌ O lote excederia {vc.VIDEO_MAX_BATCH_MB} MB.")
        return AGUARDANDO_LINK
    items.append(await _persist_input(update, {"video_bytes": data, "filename": filename, "size_mb": size}))
    ctx.user_data["dl_videos"] = items
    await msg.reply_text(f"✅ {len(items)}/10 — {filename}", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def on_biblioteca(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data or ""
    if data.startswith("lib:select:"):
        ctx.user_data.pop("dl_videos", None)
    state = await legacy.on_biblioteca(update, ctx)
    if data.startswith("lib:select:") and state == AGUARDANDO_LINK:
        _batch(ctx)
    return state


async def receber_watermark(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if text and text != "/pular": edits["watermark"] = text
    else: edits.pop("watermark", None)
    await update.message.reply_text("✅ Marca atualizada.", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def receber_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if text and text != "/pular": edits["caption"] = text
    else: edits.pop("caption", None)
    await update.message.reply_text("✅ Legenda atualizada.", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def receber_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if not text or text == "/pular":
        edits.pop("crop_start", None); edits.pop("crop_end", None)
        await update.message.reply_text("⏭ Sem corte de tempo.", reply_markup=_menu(ctx))
        return AGUARDANDO_LINK
    try:
        a, b = text.split("-", 1); start, end = float(a), float(b)
        if start < 0 or end <= start: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Use `5-30`, com fim maior que início.", parse_mode="Markdown")
        return AGUARDANDO_CROP
    edits["crop_start"], edits["crop_end"] = start, end
    await update.message.reply_text(f"✅ Corte: {start:g}s–{end:g}s", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def receber_speed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    edits = ctx.user_data.setdefault("dl_edits", {})
    if not text or text == "/pular":
        edits.pop("speed", None)
        await update.message.reply_text("⏭ Velocidade normal.", reply_markup=_menu(ctx))
        return AGUARDANDO_LINK
    try:
        speed = float(text)
        if not 0.25 <= speed <= 4.0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Use um valor entre 0.25 e 4.0.")
        return AGUARDANDO_SPEED
    edits["speed"] = speed
    await update.message.reply_text(f"✅ Velocidade: {speed:g}x", reply_markup=_menu(ctx))
    return AGUARDANDO_LINK


async def on_dl_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    raw = (query.data or "").replace("dl:", "", 1)

    if raw.startswith("editor_apply:"):
        await query.answer()
        token = raw.split(":", 1)[1]
        if token != ctx.user_data.get("dl_editor_token"):
            await query.edit_message_text("❌ Sessão do editor expirada. Abra novamente.")
            return AGUARDANDO_LINK
        await query.edit_message_text("⏳ Lendo ajustes do editor...")
        editor = await asyncio.to_thread(_editor_result, token)
        if not editor.get("ok"):
            await query.message.reply_text(f"❌ {editor.get('error')}")
            return AGUARDANDO_LINK
        editable = {k: v for k, v in (editor.get("config") or {}).items() if k in video_settings.DEFAULTS}
        try:
            cfg = await asyncio.to_thread(video_settings.set_values, _uid(update), editable)
        except ValueError as exc:
            await query.message.reply_text(f"❌ Configuração inválida: {exc}")
            return AGUARDANDO_LINK
        return await _process_all(update, ctx, query.message, cfg, editor.get("items") or [])

    action = raw.split(":", 1)[0]
    if action == "cancelar":
        await query.answer(); ctx.user_data.clear(); await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END
    if action == "addlinks":
        await query.answer(); await query.edit_message_text(f"➕ Envie mais links. Lote atual: {len(_batch(ctx))}/10.")
        return AGUARDANDO_LINK
    if action == "speed":
        await query.answer(); await query.edit_message_text("⏩ Digite a velocidade (0.25–4.0), ex.: `1.5`, ou /pular.", parse_mode="Markdown")
        return AGUARDANDO_SPEED
    if action == "flip":
        await query.answer(); edits = ctx.user_data.setdefault("dl_edits", {}); edits["flip"] = not bool(edits.get("flip"))
        await query.edit_message_text("🔄 Espelhamento " + ("ativado." if edits["flip"] else "desativado."), reply_markup=_menu(ctx))
        return AGUARDANDO_LINK
    if action == "editor":
        await query.answer()
        items = _batch(ctx)
        if not items:
            await query.edit_message_text("❌ Nenhum vídeo carregado.")
            return AGUARDANDO_LINK
        sync = await _sync_fundo(update)
        if not sync.get("ok"):
            await query.edit_message_text(f"❌ {sync.get('error')}")
            return AGUARDANDO_LINK
        await query.edit_message_text(f"⏳ Preparando {len(items)} vídeo(s) no editor...")
        sources = []
        for item in items:
            data = await _load(item)
            if not data:
                await query.message.reply_text(f"❌ {item['filename']} ficou indisponível.")
                return AGUARDANDO_LINK
            sources.append((data, item["filename"]))
        result = await asyncio.to_thread(_editor_session, sources, str(_uid(update)), video_settings.get_config(_uid(update)))
        sources = None
        if not result.get("ok"):
            await query.message.reply_text(f"❌ Editor: {result.get('error')}")
            return AGUARDANDO_LINK
        token = result["token"]
        ctx.user_data["dl_editor_token"] = token
        await query.message.reply_text(
            f"🖼️ *Editor em massa — {len(items)} vídeo(s)*\n\n"
            "Use a faixa superior para trocar de vídeo. Tamanho, posição e recorte são "
            "individuais para cada vídeo e aparecem imediatamente na aba Posicionar.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖐️ Abrir editor", url=result["editor_url"])],
                [InlineKeyboardButton("✅ Aplicar e processar todos", callback_data=f"dl:editor_apply:{token}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="dl:cancelar")],
            ]),
            parse_mode="Markdown",
        )
        return AGUARDANDO_LINK
    if action == "processar":
        await query.answer(); await query.edit_message_text("⏳ Iniciando processamento do lote...")
        return await _process_all(update, ctx, query.message, video_settings.get_config(_uid(update)))

    # Fundo / watermark / caption / crop / biblioteca usam os prompts já existentes.
    return await legacy.on_dl_action(update, ctx)


def register_video_handlers(app):
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("download", cmd_download), CommandHandler("biblioteca", legacy.cmd_biblioteca)],
        states={
            AGUARDANDO_LINK: [
                CallbackQueryHandler(on_dl_action, pattern=r"^dl:"),
                CallbackQueryHandler(on_biblioteca, pattern=r"^lib:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_links),
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, receber_mp4),
            ],
            AGUARDANDO_FUNDO_DL: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, legacy.receber_fundo_dl),
                MessageHandler(filters.TEXT, legacy.receber_fundo_dl),
            ],
            AGUARDANDO_WATERMARK: [MessageHandler(filters.TEXT, receber_watermark)],
            AGUARDANDO_CAPTION: [MessageHandler(filters.TEXT, receber_caption)],
            AGUARDANDO_CROP: [MessageHandler(filters.TEXT, receber_crop)],
            AGUARDANDO_SPEED: [MessageHandler(filters.TEXT, receber_speed)],
        },
        fallbacks=[CommandHandler("cancelar", legacy.cancelar)],
        per_user=True, per_message=False,
    ))

    # Demais comandos do módulo de vídeo permanecem exatamente como antes.
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("video_lote", legacy.cmd_video_lote)],
        states={legacy.AGUARDANDO_LOTE: [
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, legacy.coletar_lote),
            CommandHandler("processar_lote", legacy.executar_lote),
        ]},
        fallbacks=[CommandHandler("cancelar", legacy.cancelar)], per_user=True, per_message=False,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("fundo", legacy.cmd_fundo)],
        states={legacy.AGUARDANDO_FUNDO: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, legacy.receber_fundo_cmd)]},
        fallbacks=[CommandHandler("cancelar", legacy.cancelar)], per_user=True, per_message=False,
    ))
    app.add_handler(CommandHandler("fundos", legacy.cmd_fundos))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(legacy.on_fundo_action, pattern=r"^fnd:")],
        states={
            legacy.AGUARDANDO_FUNDO_NOME: [MessageHandler(filters.TEXT, legacy.receber_nome_fundo)],
            legacy.AGUARDANDO_FUNDO_IMAGEM: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, legacy.receber_imagem_fundo_nomeado)],
        },
        fallbacks=[CommandHandler("cancelar", legacy.cancelar)], per_user=True, per_message=False,
    ))
    app.add_handler(CommandHandler("video_status", legacy.cmd_video_status))
    app.add_handler(CommandHandler("config_video", legacy.cmd_config_video))
    app.add_handler(CommandHandler("config_video_reset", legacy.cmd_config_video_reset))
    app.add_handler(CommandHandler("video_limpar", legacy.cmd_video_limpar))