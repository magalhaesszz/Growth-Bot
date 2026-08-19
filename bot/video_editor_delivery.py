import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
)

from bot.access import has_access
import video_client as vc
import video_settings

logger = logging.getLogger(__name__)


def _processing_config(editor_config: dict, persisted_config: dict) -> dict:
    """Combina config persistente com ajustes transitórios do editor visual."""
    config = dict(persisted_config)
    # manual_crop pertence somente à sessão visual e não faz parte das
    # preferências globais persistidas em video_settings.
    if "manual_crop" in editor_config:
        config["manual_crop"] = editor_config.get("manual_crop")
    return config


async def _send_processed_file(message, path: str, filename: str, size_mb: float) -> bool:
    """Entrega por vídeo e usa documento como fallback sem carregar bytes na RAM."""
    try:
        with open(path, "rb") as output_file:
            await message.reply_video(
                video=output_file,
                filename=filename,
                caption=f"✅ Pronto! {size_mb} MB",
            )
        return True
    except Exception as exc:
        logger.warning(
            "Falha ao enviar video processado como video (%s); tentando documento.",
            type(exc).__name__,
        )

    try:
        with open(path, "rb") as output_file:
            await message.reply_document(
                document=output_file,
                filename=filename,
                caption=f"✅ Pronto! {size_mb} MB",
            )
        return True
    except Exception as exc:
        logger.error(
            "Falha ao entregar video processado no Telegram: %s",
            type(exc).__name__,
        )
        return False


async def _handle_editor_apply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if not update.effective_user or not has_access(update.effective_user.id):
        await query.answer("Acesso negado.", show_alert=True)
        return

    await query.answer()
    raw = query.data or ""
    token = raw.replace("dl:editor_apply:", "", 1)
    stored = ctx.user_data.get("dl_editor_token")
    source = ctx.user_data.get("dl_editor_source")

    # Um botão de uma sessão antiga não deve processar o vídeo de uma sessão
    # mais nova nem apagar o estado dela.
    if not source or not stored or token != stored:
        await query.edit_message_text(
            "❌ Sessão do editor expirada. Abra o editor visual novamente."
        )
        return

    output_path = ""
    try:
        await query.edit_message_text("⏳ Buscando configurações do editor...")
        editor = await asyncio.to_thread(vc.obter_editor_result, token)
        if not editor.get("ok"):
            await query.edit_message_text(
                "❌ Editor expirado. Abra o editor novamente, salve e toque em Aplicar."
            )
            return

        editor_config = editor.get("config") or {}
        editable = {
            key: value
            for key, value in editor_config.items()
            if key in video_settings.DEFAULTS
        }
        persisted = await asyncio.to_thread(
            video_settings.set_values,
            update.effective_user.id,
            editable,
        )
        process_config = _processing_config(editor_config, persisted)

        video_bytes, filename = source
        await query.edit_message_text("⏳ Aplicando layout e processando...")
        result = await asyncio.to_thread(
            vc.processar_video_arquivo,
            video_bytes,
            filename,
            str(update.effective_user.id),
            process_config,
        )

        # O original já está salvo na biblioteca. Libera a cópia em RAM antes
        # de começar o upload do resultado ao Telegram.
        ctx.user_data.pop("dl_editor_source", None)
        ctx.user_data.pop("dl_video_bytes", None)
        source = None
        video_bytes = None

        if not result.get("ok"):
            await query.edit_message_text(
                f"❌ Falha ao processar vídeo: {result.get('error', 'erro desconhecido')}"
            )
            return

        output_path = result["video_path"]
        delivered = await _send_processed_file(
            query.message,
            output_path,
            result["filename"],
            result["size_mb"],
        )
        if delivered:
            await query.edit_message_text("✅ Vídeo processado e enviado.")
        else:
            await query.edit_message_text(
                "❌ O vídeo foi processado, mas o Telegram recusou o envio. Tente novamente."
            )
    except Exception as exc:
        logger.exception("Erro no fluxo de entrega do editor visual: %s", type(exc).__name__)
        try:
            await query.edit_message_text(
                "❌ O processamento falhou antes da entrega. Tente novamente."
            )
        except Exception:
            pass
    finally:
        if output_path:
            try:
                os.remove(output_path)
            except OSError:
                pass
        # Esta sessão foi consumida; não permita um segundo clique duplicar o job.
        ctx.user_data.clear()


async def _editor_apply_guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await _handle_editor_apply(update, ctx)
    finally:
        # Impede que o ConversationHandler antigo processe o mesmo callback de
        # novo em outro grupo e dispare um segundo render.
        raise ApplicationHandlerStop


def register_video_editor_delivery_guard(app) -> None:
    app.add_handler(
        CallbackQueryHandler(
            _editor_apply_guard,
            pattern=r"^dl:editor_apply:.+$",
        ),
        group=-6,
    )
