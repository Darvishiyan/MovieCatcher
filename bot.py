"""Telegram-based file downloader designed to complement a Jellyfin library."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Python 3.14 no longer auto-creates an event loop; Pyrogram needs one at import time.
asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client as PyrogramClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _positive_int(name: str) -> int:
    raw_value = os.getenv(name, "").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: str) -> float:
    raw_value = os.getenv(name, default).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return value


def _id_set(name: str) -> frozenset[int]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return frozenset()
    try:
        return frozenset(
            int(item.strip()) for item in raw_value.split(",") if item.strip()
        )
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be a comma-separated list of integer IDs"
        ) from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    download_root: Path
    session_dir: Path
    max_file_size_bytes: int
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    log_level: str

    @classmethod
    def from_environment(cls) -> "Settings":
        allowed_user_ids = _id_set("ALLOWED_USER_IDS")
        allowed_chat_ids = _id_set("ALLOWED_CHAT_IDS")
        if not allowed_user_ids and not allowed_chat_ids:
            raise ConfigurationError(
                "Set at least one ALLOWED_USER_IDS or ALLOWED_CHAT_IDS entry"
            )

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigurationError(f"Unsupported LOG_LEVEL: {log_level}")

        max_size_gb = _positive_float("MAX_FILE_SIZE_GB", "10")
        return cls(
            bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
            api_id=_positive_int("TELEGRAM_API_ID"),
            api_hash=_required_env("TELEGRAM_API_HASH"),
            download_root=Path(_required_env("DOWNLOAD_ROOT")).expanduser().resolve(),
            session_dir=Path(os.getenv("SESSION_DIR", "/data")).expanduser().resolve(),
            max_file_size_bytes=int(max_size_gb * 1024**3),
            allowed_user_ids=allowed_user_ids,
            allowed_chat_ids=allowed_chat_ids,
            log_level=log_level,
        )


try:
    SETTINGS = Settings.from_environment()
except ConfigurationError as exc:
    raise SystemExit(f"Configuration error: {exc}") from None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, SETTINGS.log_level),
)
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

SETTINGS.download_root.mkdir(parents=True, exist_ok=True)
SETTINGS.session_dir.mkdir(parents=True, exist_ok=True)

INVALID_FOLDER_CHARS = frozenset('/\\:*?"<>|')


def _is_authorized(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return bool(
        (user_id is not None and user_id in SETTINGS.allowed_user_ids)
        or (chat_id is not None and chat_id in SETTINGS.allowed_chat_ids)
    )


def _is_within_download_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(SETTINGS.download_root)
        return True
    except ValueError:
        return False


def _subdirectories(current_dir: Path) -> list[Path]:
    try:
        return sorted(
            (
                entry
                for entry in current_dir.iterdir()
                if entry.is_dir() and _is_within_download_root(entry)
            ),
            key=lambda entry: entry.name.casefold(),
        )
    except OSError:
        logger.exception("Could not list download directory: %s", current_dir)
        return []


def _safe_file_name(file_name: str, fallback: str) -> str:
    normalized = Path(file_name.replace("\x00", "")).name.strip()
    return normalized if normalized not in {"", ".", ".."} else fallback


def _keyboard(current_dir: Path) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if current_dir != SETTINGS.download_root:
        buttons.append([InlineKeyboardButton("⬆️ Go up", callback_data="up")])
    for index, directory in enumerate(_subdirectories(current_dir)):
        buttons.append(
            [InlineKeyboardButton(f"📁 {directory.name}", callback_data=f"cd:{index}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("✅ Download here", callback_data="dl"),
            InlineKeyboardButton("📁+ New folder", callback_data="nf"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _display_path(current_dir: Path) -> str:
    relative_path = current_dir.relative_to(SETTINGS.download_root)
    return "/" if relative_path == Path(".") else f"/{relative_path.as_posix()}"


def _directory_text(current_dir: Path) -> str:
    display_path = html.escape(_display_path(current_dir))
    return f"📂 <code>{display_path}</code>\n\nNavigate or download here:"


def _progress_bar(percent: float, width: int = 20) -> str:
    filled = min(width, max(0, int(width * percent / 100)))
    return "█" * filled + "░" * (width - filled)


async def _reject_unauthorized(update: Update) -> bool:
    if _is_authorized(update):
        return False
    logger.warning(
        "Rejected unauthorized request from user_id=%s chat_id=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_chat.id if update.effective_chat else None,
    )
    if update.callback_query:
        await update.callback_query.answer("Not authorized", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Not authorized.")
    return True


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return

    message = update.message
    if message is None or context.user_data.get("state") == "waiting_folder":
        return

    attachment = (
        message.document
        or message.video
        or message.audio
        or message.animation
        or message.voice
        or message.video_note
        or (message.photo[-1] if message.photo else None)
    )
    if attachment is None:
        return

    file_id = attachment.file_id
    extension = (
        mimetypes.guess_extension(getattr(attachment, "mime_type", "") or "") or ""
    )
    fallback = f"file_{attachment.file_unique_id}{extension}"
    file_name = _safe_file_name(
        getattr(attachment, "file_name", None) or fallback, fallback
    )
    file_size = getattr(attachment, "file_size", 0) or 0

    if file_size > SETTINGS.max_file_size_bytes:
        limit_gb = SETTINGS.max_file_size_bytes / 1024**3
        size_gb = file_size / 1024**3
        await message.reply_text(
            f"File too large: <b>{size_gb:.2f} GB</b> (limit is {limit_gb:g} GB).",
            parse_mode="HTML",
        )
        return

    context.user_data.update(
        {
            "file_id": file_id,
            "file_name": file_name,
            "file_size": file_size,
            "current_dir": SETTINGS.download_root,
            "state": "browsing",
        }
    )

    await message.reply_text(
        f"Received: <b>{html.escape(file_name)}</b>\n\n"
        f"{_directory_text(SETTINGS.download_root)}",
        parse_mode="HTML",
        reply_markup=_keyboard(SETTINGS.download_root),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return

    message = update.message
    if message is None or message.text is None:
        return

    if context.user_data.get("state") == "waiting_folder":
        folder_name = message.text.strip()
        if (
            not folder_name
            or folder_name in {".", ".."}
            or any(character in folder_name for character in INVALID_FOLDER_CHARS)
            or "\x00" in folder_name
        ):
            await message.reply_text("Invalid name. Try again:")
            return

        current_dir = Path(context.user_data["current_dir"]).resolve()
        new_dir = (current_dir / folder_name).resolve()
        if not _is_within_download_root(new_dir):
            await message.reply_text("That folder would be outside the download root.")
            return

        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Could not create directory: %s", new_dir)
            await message.reply_text(
                "Could not create that folder. Check container permissions."
            )
            return

        context.user_data["current_dir"] = new_dir
        context.user_data["state"] = "browsing"
        await message.reply_text(
            f"Created <b>{html.escape(folder_name)}</b>\n\n{_directory_text(new_dir)}",
            parse_mode="HTML",
            reply_markup=_keyboard(new_dir),
        )
        return

async def _download_telegram_media(
    info: dict[str, Any],
    destination: Path,
    status_message: Any,
    client: PyrogramClient,
) -> Path:
    known_total = int(info.get("file_size", 0))
    last_update_time = [time.monotonic()]
    last_update_bytes = [0]

    async def on_progress(current: int, total: int) -> None:
        total = total or known_total
        if not total:
            return
        now = time.monotonic()
        percent = current * 100 / total
        elapsed = now - last_update_time[0]
        if elapsed < 3.0 and percent < 99.9:
            return
        speed = (
            (current - last_update_bytes[0]) / elapsed / 1024 / 1024
            if elapsed > 0
            else 0
        )
        last_update_time[0] = now
        last_update_bytes[0] = current
        try:
            await status_message.edit_text(
                f"<b>{html.escape(info['file_name'])}</b>\n\n"
                f"{_progress_bar(percent)}\n"
                f"{percent:.0f}%  •  {current / 1024 / 1024:.1f} / "
                f"{total / 1024 / 1024:.1f} MB  •  {speed:.1f} MB/s",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("Progress update was skipped: %s", exc)

    result = await client.download_media(
        info["file_id"], file_name=str(destination), progress=on_progress
    )
    if not result:
        raise RuntimeError("Telegram returned no downloaded file")
    return Path(result)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if not context.user_data.get("state"):
        await query.edit_message_text("Session expired. Please send the file again.")
        return

    current_dir = Path(context.user_data["current_dir"]).resolve()
    if not _is_within_download_root(current_dir):
        logger.error("Rejected stored path outside download root: %s", current_dir)
        context.user_data.clear()
        await query.edit_message_text("Invalid destination. Please send the item again.")
        return

    action = query.data or ""
    if action == "up":
        if current_dir != SETTINGS.download_root:
            current_dir = current_dir.parent
            context.user_data["current_dir"] = current_dir
        await query.edit_message_text(
            _directory_text(current_dir),
            parse_mode="HTML",
            reply_markup=_keyboard(current_dir),
        )
        return

    if action.startswith("cd:"):
        try:
            index = int(action[3:])
        except ValueError:
            await query.edit_message_text("Invalid folder selection.")
            return
        subdirectories = _subdirectories(current_dir)
        if not 0 <= index < len(subdirectories):
            await query.edit_message_text(
                "The folder list changed. Please send the item again."
            )
            return
        current_dir = subdirectories[index].resolve()
        context.user_data["current_dir"] = current_dir
        await query.edit_message_text(
            _directory_text(current_dir),
            parse_mode="HTML",
            reply_markup=_keyboard(current_dir),
        )
        return

    if action == "nf":
        context.user_data["state"] = "waiting_folder"
        await query.edit_message_text(
            f"📂 <code>{html.escape(_display_path(current_dir))}</code>\n\n"
            "Type the new folder name:",
            parse_mode="HTML",
        )
        return

    if action != "dl":
        await query.edit_message_text("Unknown action. Please send the item again.")
        return

    info = dict(context.user_data)
    context.user_data.clear()
    try:
        current_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Could not prepare download directory: %s", current_dir)
        await query.edit_message_text(
            "Could not prepare that folder. Check container permissions."
        )
        return

    destination = current_dir / info["file_name"]
    if destination.exists():
        await query.edit_message_text(
            f"⚠️ Already exists, skipped.\n\n"
            f"<code>{html.escape(_display_path(destination))}</code>",
            parse_mode="HTML",
        )
        return
    status_text = f"<b>{html.escape(info['file_name'])}</b>\n\n0%  •  0.0 MB/s"

    await query.message.delete()
    status_message = await query.message.reply_text(status_text, parse_mode="HTML")

    try:
        client = context.bot_data.get("pyrogram_client")
        if not isinstance(client, PyrogramClient):
            raise RuntimeError("Telegram download client is unavailable")
        downloaded_path = await _download_telegram_media(
            info, destination, status_message, client
        )
        logger.info("Download complete: %s", downloaded_path)
    except Exception as exc:
        logger.exception("Download failed")
        error_message = html.escape(str(exc)[:3500])
        try:
            await status_message.edit_text(
                f"❌ Download failed: {error_message}", parse_mode="HTML"
            )
        except Exception:
            logger.exception("Could not update the failure status message")
        return

    try:
        display_path = _display_path(downloaded_path.resolve())
    except ValueError:
        display_path = _display_path(current_dir)
    await status_message.edit_text(
        f"✅ Done!\n\nLocation: <code>{html.escape(display_path)}</code>",
        parse_mode="HTML",
    )


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, stop_event.set)
        except NotImplementedError:
            pass

    pyrogram_client = PyrogramClient(
        "bot_session",
        workdir=str(SETTINGS.session_dir),
        api_id=SETTINGS.api_id,
        api_hash=SETTINGS.api_hash,
        bot_token=SETTINGS.bot_token,
        no_updates=True,
    )
    await pyrogram_client.start()
    logger.info("Pyrogram client started")
    try:
        application = Application.builder().token(SETTINGS.bot_token).build()
        application.bot_data["pyrogram_client"] = pyrogram_client
        attachment_filter = (
            filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.ANIMATION
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.PHOTO
        )
        application.add_handler(MessageHandler(attachment_filter, handle_media))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
        )
        application.add_handler(CallbackQueryHandler(handle_callback))

        async with application:
            await application.start()
            if application.updater is None:
                raise RuntimeError("Telegram updater is unavailable")
            updater_started = False
            try:
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                updater_started = True
                logger.info("MovieCatcher started")
                await stop_event.wait()
            finally:
                if updater_started:
                    await application.updater.stop()
                await application.stop()
    finally:
        await pyrogram_client.stop()
        logger.info("MovieCatcher stopped")


if __name__ == "__main__":
    asyncio.run(main())
