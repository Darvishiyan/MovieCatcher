"""Telegram-based file downloader designed to complement a Jellyfin library."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
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


@dataclass
class QueuedDownload:
    """A Telegram file waiting for the single download worker."""

    job_id: str
    file_id: str
    file_name: str
    file_size: int
    destination_dir: Path
    chat_id: int
    owner_user_id: int | None
    batch_id: str | None = None
    queue_message_id: int | None = None
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "queued"


@dataclass(frozen=True)
class PendingFile:
    """A received Telegram attachment waiting for destination selection."""

    file_id: str
    file_name: str
    file_size: int


class DownloadCancelled(RuntimeError):
    """Raised when a user deliberately cancels a Telegram download."""


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
MEDIA_GROUP_SETTLE_SECONDS = 1.0
PENDING_FILE_DISPLAY_LIMIT = 20
PENDING_DOWNLOAD_KEYS = (
    "file_id",
    "file_name",
    "file_size",
    "pending_files",
    "media_group_id",
    "media_group_task",
    "current_dir",
    "folder_prompt_message_id",
    "selection_chat_id",
    "selection_message_id",
    "state",
)


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


def _short_file_name(file_name: str, limit: int = 100) -> str:
    if len(file_name) <= limit:
        return file_name
    side_length = (limit - 1) // 2
    return f"{file_name[:side_length]}…{file_name[-side_length:]}"


def _initial_directory(user_data: dict[str, Any]) -> Path:
    """Return the user's last valid destination, falling back to the root."""

    last_dir = Path(user_data.get("last_dir", SETTINGS.download_root)).resolve()
    if not _is_within_download_root(last_dir) or not last_dir.is_dir():
        return SETTINGS.download_root
    return last_dir


def _clear_pending_download(user_data: dict[str, Any]) -> None:
    """Clear folder-selection state without forgetting the last destination."""

    media_group_task = user_data.get("media_group_task")
    if isinstance(media_group_task, asyncio.Task):
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if media_group_task is not current_task and not media_group_task.done():
            media_group_task.cancel()
    for key in PENDING_DOWNLOAD_KEYS:
        user_data.pop(key, None)


async def _delete_folder_prompt(context: Any, chat_id: int | None) -> None:
    """Remove the folder-name prompt after a folder is created successfully."""

    message_id = context.user_data.pop("folder_prompt_message_id", None)
    if message_id is None or chat_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("Could not delete folder-name prompt: %s", exc)


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
    buttons.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_selection")]
    )
    return InlineKeyboardMarkup(buttons)


def _cancel_download_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel download", callback_data=f"cancel:{job_id}")]]
    )


def _cancel_batch_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel batch",
                    callback_data=f"cancel_batch:{batch_id}",
                )
            ]
        ]
    )


async def _delete_queue_message(
    application: Application,
    item: QueuedDownload,
) -> None:
    """Remove a stale queue notice once its download status is visible."""

    message_id = item.queue_message_id
    if message_id is None:
        return

    # Every item in a media batch shares one queue notice. Clear the shared
    # reference first so later items do not try to delete the same message.
    downloads: dict[str, QueuedDownload] = application.bot_data.get("downloads", {})
    for download in downloads.values():
        if (
            download.chat_id == item.chat_id
            and download.queue_message_id == message_id
        ):
            download.queue_message_id = None

    try:
        await application.bot.delete_message(
            chat_id=item.chat_id,
            message_id=message_id,
        )
    except Exception as exc:
        logger.debug("Could not delete queue status message: %s", exc)


def _display_path(current_dir: Path) -> str:
    relative_path = current_dir.relative_to(SETTINGS.download_root)
    return "/" if relative_path == Path(".") else f"/{relative_path.as_posix()}"


def _directory_text(current_dir: Path) -> str:
    display_path = html.escape(_display_path(current_dir))
    return f"📂 <code>{display_path}</code>\n\nNavigate or download here:"


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


def _pending_file_from_message(message: Any) -> PendingFile | None:
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
        return None

    extension = (
        mimetypes.guess_extension(getattr(attachment, "mime_type", "") or "") or ""
    )
    fallback = f"file_{attachment.file_unique_id}{extension}"
    return PendingFile(
        file_id=attachment.file_id,
        file_name=_safe_file_name(
            getattr(attachment, "file_name", None) or fallback,
            fallback,
        ),
        file_size=getattr(attachment, "file_size", 0) or 0,
    )


def _pending_files_heading(pending_files: list[PendingFile]) -> str:
    if len(pending_files) == 1:
        return f"Received: <b>{html.escape(pending_files[0].file_name)}</b>"

    displayed_files = pending_files[:PENDING_FILE_DISPLAY_LIMIT]
    file_list = "\n".join(
        f"• <code>{html.escape(_short_file_name(item.file_name))}</code>"
        for item in displayed_files
    )
    remaining_count = len(pending_files) - len(displayed_files)
    if remaining_count:
        file_list += f"\n• …and {remaining_count} more"
    total_size = sum(item.file_size for item in pending_files)
    return (
        f"Received <b>{len(pending_files)} files</b>:\n{file_list}\n\n"
        f"Total size: <b>{total_size / 1024 / 1024:.1f} MB</b>\n"
        "Choose one destination for the entire batch."
    )


def _pending_files_text(
    pending_files: list[PendingFile],
    current_dir: Path,
) -> str:
    return (
        f"{_pending_files_heading(pending_files)}\n\n"
        f"{_directory_text(current_dir)}\n\n"
        "Any additional files sent before choosing a folder will join this batch."
    )


async def _present_pending_files(message: Any, context: Any) -> None:
    pending_files: list[PendingFile] = context.user_data.get("pending_files", [])
    if not pending_files:
        return

    initial_dir = _initial_directory(context.user_data)
    context.user_data.update(
        {
            "current_dir": initial_dir,
            "state": "browsing",
        }
    )
    context.user_data.pop("media_group_task", None)

    selection_message = await message.reply_text(
        _pending_files_text(pending_files, initial_dir),
        parse_mode="HTML",
        reply_markup=_keyboard(initial_dir),
    )
    context.user_data["selection_message_id"] = selection_message.message_id
    context.user_data["selection_chat_id"] = getattr(
        selection_message,
        "chat_id",
        getattr(message, "chat_id", None),
    )


async def _refresh_pending_files(message: Any, context: Any) -> None:
    """Add loose consecutive files to the currently open folder selection."""

    pending_files: list[PendingFile] = context.user_data["pending_files"]
    current_dir = Path(context.user_data["current_dir"]).resolve()
    message_id = context.user_data.get("selection_message_id")
    chat_id = context.user_data.get("selection_chat_id")
    if message_id is not None and chat_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_pending_files_text(pending_files, current_dir),
                parse_mode="HTML",
                reply_markup=_keyboard(current_dir),
            )
            return
        except Exception as exc:
            logger.debug("Could not refresh the pending-file prompt: %s", exc)

    selection_message = await message.reply_text(
        _pending_files_text(pending_files, current_dir),
        parse_mode="HTML",
        reply_markup=_keyboard(current_dir),
    )
    context.user_data["selection_message_id"] = selection_message.message_id
    context.user_data["selection_chat_id"] = getattr(
        selection_message,
        "chat_id",
        getattr(message, "chat_id", None),
    )


async def _present_media_group_after_delay(
    message: Any,
    context: Any,
    media_group_id: str,
) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_SETTLE_SECONDS)
    except asyncio.CancelledError:
        return
    if (
        context.user_data.get("state") == "collecting"
        and context.user_data.get("media_group_id") == media_group_id
    ):
        await _present_pending_files(message, context)


def _schedule_media_group_prompt(
    message: Any,
    context: Any,
    media_group_id: str,
) -> None:
    previous_task = context.user_data.get("media_group_task")
    if isinstance(previous_task, asyncio.Task) and not previous_task.done():
        previous_task.cancel()
    context.user_data["media_group_task"] = context.application.create_task(
        _present_media_group_after_delay(message, context, media_group_id)
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return

    message = update.message
    if message is None:
        return

    pending_file = _pending_file_from_message(message)
    if pending_file is None:
        return

    if pending_file.file_size > SETTINGS.max_file_size_bytes:
        limit_gb = SETTINGS.max_file_size_bytes / 1024**3
        size_gb = pending_file.file_size / 1024**3
        await message.reply_text(
            f"File too large: <b>{html.escape(pending_file.file_name)}</b> "
            f"is {size_gb:.2f} GB (limit is {limit_gb:g} GB).",
            parse_mode="HTML",
        )
        return

    media_group_id = getattr(message, "media_group_id", None)
    state = context.user_data.get("state")
    active_media_group_id = context.user_data.get("media_group_id")

    if state == "collecting" and (
        media_group_id is None or active_media_group_id == media_group_id
    ):
        context.user_data["pending_files"].append(pending_file)
        _schedule_media_group_prompt(message, context, active_media_group_id)
        return

    if state == "browsing":
        context.user_data["pending_files"].append(pending_file)
        await _refresh_pending_files(message, context)
        return

    if state in {"collecting", "waiting_folder"}:
        await message.reply_text(
            "Please finish or cancel the previous folder selection first."
        )
        return

    context.user_data["pending_files"] = [pending_file]
    if media_group_id is None:
        await _present_pending_files(message, context)
        return

    context.user_data.update(
        {
            "media_group_id": media_group_id,
            "state": "collecting",
        }
    )
    _schedule_media_group_prompt(message, context, media_group_id)


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

        await _delete_folder_prompt(
            context,
            update.effective_chat.id if update.effective_chat is not None else None,
        )
        context.user_data["current_dir"] = new_dir
        context.user_data["state"] = "browsing"
        selection_message = await message.reply_text(
            f"Created <b>{html.escape(folder_name)}</b>\n\n{_directory_text(new_dir)}",
            parse_mode="HTML",
            reply_markup=_keyboard(new_dir),
        )
        context.user_data["selection_message_id"] = selection_message.message_id
        context.user_data["selection_chat_id"] = getattr(
            selection_message,
            "chat_id",
            getattr(message, "chat_id", None),
        )
        return


async def _download_telegram_media(
    item: QueuedDownload,
    destination: Path,
    status_message: Any,
    client: PyrogramClient,
) -> Path:
    known_total = item.file_size
    last_update_time = [time.monotonic()]
    last_update_bytes = [0]

    async def on_progress(current: int, total: int) -> None:
        if item.cancel_requested.is_set():
            # Pyrogram requires stop_transmission() to be called from its
            # progress callback. It then removes the partial .temp file.
            client.stop_transmission()

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
                f"⬇️ <b>{html.escape(item.file_name)}</b>\n\n"
                f"{percent:.0f}%  •  {current / 1024 / 1024:.1f} / "
                f"{total / 1024 / 1024:.1f} MB  •  {speed:.1f} MB/s",
                parse_mode="HTML",
                reply_markup=_cancel_download_keyboard(item.job_id),
            )
        except Exception as exc:
            logger.debug("Text progress update was skipped: %s", exc)

    result = await client.download_media(
        item.file_id,
        file_name=str(destination),
        progress=on_progress,
    )
    if not result:
        if item.cancel_requested.is_set():
            raise DownloadCancelled
        raise RuntimeError("Telegram returned no downloaded file")
    return Path(result)


async def _process_queued_download(
    application: Application,
    item: QueuedDownload,
) -> None:
    """Download one queued item and report its lifecycle in Telegram."""

    if item.cancel_requested.is_set():
        item.state = "cancelled"
        return

    current_dir = item.destination_dir.resolve()
    if not _is_within_download_root(current_dir):
        logger.error("Rejected queued path outside download root: %s", current_dir)
        await application.bot.send_message(
            chat_id=item.chat_id,
            text="❌ The queued destination is no longer valid.",
        )
        return

    try:
        current_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Could not prepare queued directory: %s", current_dir)
        await application.bot.send_message(
            chat_id=item.chat_id,
            text="❌ Could not prepare that folder. Check container permissions.",
        )
        return

    destination = current_dir / item.file_name
    display_destination = html.escape(_display_path(destination))
    if destination.exists():
        item.state = "skipped"
        await application.bot.send_message(
            chat_id=item.chat_id,
            text=(
                "⚠️ Already exists, skipped.\n\n"
                f"<code>{display_destination}</code>"
            ),
            parse_mode="HTML",
        )
        return

    item.state = "downloading"
    status_message = await application.bot.send_message(
        chat_id=item.chat_id,
        text=(
            f"⬇️ Download started: <b>{html.escape(item.file_name)}</b>\n\n"
            f"Destination: <code>{html.escape(_display_path(current_dir))}</code>"
        ),
        parse_mode="HTML",
        reply_markup=_cancel_download_keyboard(item.job_id),
    )
    await _delete_queue_message(application, item)

    client = application.bot_data.get("pyrogram_client")
    if client is None:
        raise RuntimeError("Telegram download client is unavailable")

    try:
        downloaded_path = await _download_telegram_media(
            item,
            destination,
            status_message,
            client,
        )
        logger.info("Download complete: %s", downloaded_path)
    except asyncio.CancelledError:
        raise
    except DownloadCancelled:
        item.state = "cancelled"
        logger.info("Download cancelled: %s", destination)
        await status_message.edit_text(
            text=f"❌ Cancelled: <b>{html.escape(item.file_name)}</b>",
            parse_mode="HTML",
        )
        return
    except Exception as exc:
        item.state = "failed"
        logger.exception("Download failed")
        error_message = html.escape(str(exc)[:3500])
        await status_message.edit_text(
            text=f"❌ Download failed: {error_message}",
            parse_mode="HTML",
        )
        return

    try:
        display_path = _display_path(downloaded_path.resolve())
    except ValueError:
        display_path = _display_path(current_dir)
    item.state = "completed"
    await status_message.edit_text(
        text=f"✅ Done!\n\nLocation: <code>{html.escape(display_path)}</code>",
        parse_mode="HTML",
    )


async def _download_worker(application: Application) -> None:
    """Process the global FIFO queue one file at a time."""

    queue: asyncio.Queue[QueuedDownload] = application.bot_data["download_queue"]
    downloads: dict[str, QueuedDownload] = application.bot_data["downloads"]
    while True:
        item = await queue.get()
        application.bot_data["download_active"] = True
        try:
            await _process_queued_download(application, item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error while processing queued download")
        finally:
            application.bot_data["download_active"] = False
            downloads.pop(item.job_id, None)
            queue.task_done()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return

    query = update.callback_query
    if query is None:
        return
    action = query.data or ""

    if action.startswith("cancel_batch:"):
        batch_id = action.removeprefix("cancel_batch:")
        downloads: dict[str, QueuedDownload] = context.bot_data["downloads"]
        items = [
            item for item in downloads.values() if item.batch_id == batch_id
        ]
        if not items:
            await query.answer("This batch has already finished.", show_alert=True)
            return

        effective_user_id = (
            update.effective_user.id if update.effective_user is not None else None
        )
        if any(
            item.owner_user_id is not None
            and item.owner_user_id != effective_user_id
            for item in items
        ):
            await query.answer("Only the sender can cancel this batch.", show_alert=True)
            return

        await query.answer()
        active_count = 0
        for item in items:
            item.cancel_requested.set()
            if item.state == "queued":
                item.state = "cancelled"
            elif item.state == "downloading":
                active_count += 1
        if active_count:
            message = (
                f"⏳ Cancelling batch of <b>{len(items)} files</b>…\n"
                "The active transfer will stop at its next progress update."
            )
        else:
            message = f"❌ Cancelled batch of <b>{len(items)} files</b>."
        await query.edit_message_text(message, parse_mode="HTML")
        return

    if action.startswith("cancel:"):
        job_id = action.removeprefix("cancel:")
        downloads: dict[str, QueuedDownload] = context.bot_data["downloads"]
        item = downloads.get(job_id)
        if item is None:
            await query.answer("This download has already finished.", show_alert=True)
            return

        effective_user_id = (
            update.effective_user.id if update.effective_user is not None else None
        )
        if item.owner_user_id is not None and item.owner_user_id != effective_user_id:
            await query.answer("Only the sender can cancel this download.", show_alert=True)
            return

        await query.answer()
        item.cancel_requested.set()
        if item.state == "queued":
            item.state = "cancelled"
            message = f"❌ Cancelled: <b>{html.escape(item.file_name)}</b>"
        else:
            message = f"⏳ Cancelling: <b>{html.escape(item.file_name)}</b>…"
        await query.edit_message_text(message, parse_mode="HTML")
        return

    await query.answer()

    if action == "cancel_selection":
        _clear_pending_download(context.user_data)
        await query.edit_message_text("❌ File selection cancelled.")
        return

    if not context.user_data.get("state"):
        await query.edit_message_text("Session expired. Please send the file again.")
        return

    current_dir = Path(context.user_data["current_dir"]).resolve()
    if not _is_within_download_root(current_dir):
        logger.error("Rejected stored path outside download root: %s", current_dir)
        _clear_pending_download(context.user_data)
        await query.edit_message_text("Invalid destination. Please send the item again.")
        return

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
        if query.message is not None:
            context.user_data["folder_prompt_message_id"] = query.message.message_id
        await query.edit_message_text(
            f"📂 <code>{html.escape(_display_path(current_dir))}</code>\n\n"
            "Type the new folder name:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_selection")]]
            ),
        )
        return

    if action != "dl":
        await query.edit_message_text("Unknown action. Please send the item again.")
        return

    if update.effective_chat is None:
        _clear_pending_download(context.user_data)
        await query.edit_message_text("Could not determine the destination chat.")
        return

    pending_files: list[PendingFile] = context.user_data.get("pending_files", [])
    if not pending_files:
        _clear_pending_download(context.user_data)
        await query.edit_message_text("No files are waiting for download.")
        return

    queue: asyncio.Queue[QueuedDownload] = context.bot_data["download_queue"]
    downloads: dict[str, QueuedDownload] = context.bot_data["downloads"]
    items_ahead = queue.qsize() + int(bool(context.bot_data["download_active"]))
    owner_user_id = (
        update.effective_user.id if update.effective_user is not None else None
    )
    batch_id = uuid.uuid4().hex[:16] if len(pending_files) > 1 else None
    items = [
        QueuedDownload(
            job_id=uuid.uuid4().hex[:16],
            file_id=pending_file.file_id,
            file_name=pending_file.file_name,
            file_size=pending_file.file_size,
            destination_dir=current_dir,
            chat_id=update.effective_chat.id,
            owner_user_id=owner_user_id,
            batch_id=batch_id,
            queue_message_id=(
                query.message.message_id if query.message is not None else None
            ),
        )
        for pending_file in pending_files
    ]

    if items_ahead:
        subject = "batch" if len(items) > 1 else "file"
        queue_text = f"{items_ahead} download(s) ahead of this {subject}."
    else:
        queue_text = "It will start shortly."
    if len(items) == 1:
        queue_heading = f"🕓 Queued: <b>{html.escape(items[0].file_name)}</b>"
        queue_markup = _cancel_download_keyboard(items[0].job_id)
    else:
        queue_heading = f"🕓 Queued batch: <b>{len(items)} files</b>"
        queue_markup = _cancel_batch_keyboard(batch_id or "")
    await query.edit_message_text(
        f"{queue_heading}\n\n"
        f"Folder: <code>{html.escape(_display_path(current_dir))}</code>\n"
        f"{queue_text}",
        parse_mode="HTML",
        reply_markup=queue_markup,
    )
    for item in items:
        downloads[item.job_id] = item
        await queue.put(item)
    context.user_data["last_dir"] = current_dir
    _clear_pending_download(context.user_data)


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
        application.bot_data["download_queue"] = asyncio.Queue()
        application.bot_data["downloads"] = {}
        application.bot_data["download_active"] = False
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
            worker_task = asyncio.create_task(
                _download_worker(application),
                name="moviecatcher-download-worker",
            )
            try:
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                updater_started = True
                logger.info("MovieCatcher started")
                await stop_event.wait()
            finally:
                if updater_started:
                    await application.updater.stop()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                await application.stop()
    finally:
        await pyrogram_client.stop()
        logger.info("MovieCatcher stopped")


if __name__ == "__main__":
    asyncio.run(main())
