import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_DIRECTORY = tempfile.mkdtemp(prefix="moviecatcher-tests-")
DOWNLOAD_ROOT = Path(TEST_DIRECTORY) / "downloads"
SESSION_ROOT = Path(TEST_DIRECTORY) / "sessions"
os.environ.update(
    {
        "TELEGRAM_BOT_TOKEN": "not-a-real-token",
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "test-api-hash-placeholder",
        "ALLOWED_USER_IDS": "100",
        "DOWNLOAD_ROOT": str(DOWNLOAD_ROOT),
        "SESSION_DIR": str(SESSION_ROOT),
    }
)

import bot


class FakeMessage:
    def __init__(self, attachment=None, message_id=50):
        self.message_id = message_id
        self.document = attachment
        self.video = None
        self.audio = None
        self.animation = None
        self.voice = None
        self.video_note = None
        self.photo = []
        self.text = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies) + 100)


class FakeQuery:
    def __init__(self, data, message_id=50):
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeStatusMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.status_messages = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        status = FakeStatusMessage()
        self.status_messages.append(status)
        return status


class SuccessfulClient:
    def __init__(self):
        self.stop_called = False

    async def download_media(self, file_id, file_name, progress):
        await progress(5 * 1024 * 1024, 10 * 1024 * 1024)
        await progress(10 * 1024 * 1024, 10 * 1024 * 1024)
        return file_name

    def stop_transmission(self):
        self.stop_called = True
        raise AssertionError("stop_transmission should not be called")


class CancelledTransmission(Exception):
    pass


class CancellingClient:
    def __init__(self, item):
        self.item = item
        self.stop_called = False

    async def download_media(self, file_id, file_name, progress):
        self.item.cancel_requested.set()
        try:
            await progress(1024, 10 * 1024)
        except CancelledTransmission:
            return None
        raise AssertionError("the progress callback did not stop the transmission")

    def stop_transmission(self):
        self.stop_called = True
        raise CancelledTransmission


def make_update(*, message=None, query=None, user_id=100, chat_id=200):
    return SimpleNamespace(
        message=message,
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message or query.message,
    )


def make_context(*, user_data=None, bot_data=None):
    return SimpleNamespace(user_data=user_data or {}, bot_data=bot_data or {})


def make_job(name="episode.mkv"):
    return bot.QueuedDownload(
        job_id="job123",
        file_id="file-id",
        file_name=name,
        file_size=10 * 1024 * 1024,
        destination_dir=DOWNLOAD_ROOT,
        chat_id=200,
        owner_user_id=100,
    )


class MovieCatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        for child in DOWNLOAD_ROOT.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    async def test_folder_browser_still_lists_directories_and_has_cancel(self):
        (DOWNLOAD_ROOT / "Movies").mkdir()
        (DOWNLOAD_ROOT / "Shows").mkdir()

        markup = bot._keyboard(DOWNLOAD_ROOT)
        buttons = [button for row in markup.inline_keyboard for button in row]
        labels = {button.text for button in buttons}
        callbacks = {button.callback_data for button in buttons}

        self.assertIn("📁 Movies", labels)
        self.assertIn("📁 Shows", labels)
        self.assertIn("cancel_selection", callbacks)

    async def test_cancel_after_sending_clears_only_pending_selection(self):
        query = FakeQuery("cancel_selection")
        user_data = {
            "state": "browsing",
            "file_id": "file-id",
            "file_name": "episode.mkv",
            "file_size": 100,
            "current_dir": DOWNLOAD_ROOT,
            "last_dir": DOWNLOAD_ROOT,
        }
        context = make_context(user_data=user_data, bot_data={"downloads": {}})

        await bot.handle_callback(make_update(query=query), context)

        self.assertEqual(user_data, {"last_dir": DOWNLOAD_ROOT})
        self.assertIn("selection cancelled", query.edits[-1][0].lower())

    async def test_new_folder_prompt_keeps_cancel_available(self):
        query = FakeQuery("nf")
        user_data = {
            "state": "browsing",
            "file_id": "file-id",
            "file_name": "episode.mkv",
            "file_size": 100,
            "current_dir": DOWNLOAD_ROOT,
        }
        context = make_context(user_data=user_data, bot_data={"downloads": {}})

        await bot.handle_callback(make_update(query=query), context)

        markup = query.edits[-1][1]["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "cancel_selection")

    async def test_confirmed_download_is_queued_with_cancel_button(self):
        query = FakeQuery("dl")
        queue = asyncio.Queue()
        downloads = {}
        user_data = {
            "state": "browsing",
            "file_id": "file-id",
            "file_name": "episode.mkv",
            "file_size": 100,
            "current_dir": DOWNLOAD_ROOT,
        }
        context = make_context(
            user_data=user_data,
            bot_data={
                "download_queue": queue,
                "downloads": downloads,
                "download_active": False,
            },
        )

        await bot.handle_callback(make_update(query=query), context)

        item = queue.get_nowait()
        queue.task_done()
        self.assertIs(downloads[item.job_id], item)
        self.assertEqual(item.state, "queued")
        markup = query.edits[-1][1]["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            f"cancel:{item.job_id}",
        )

    async def test_queued_download_can_be_cancelled(self):
        item = make_job()
        query = FakeQuery(f"cancel:{item.job_id}")
        context = make_context(bot_data={"downloads": {item.job_id: item}})

        await bot.handle_callback(make_update(query=query), context)

        self.assertTrue(item.cancel_requested.is_set())
        self.assertEqual(item.state, "cancelled")
        self.assertIn("Cancelled", query.edits[-1][0])

    async def test_active_download_cancel_requests_transfer_stop(self):
        item = make_job()
        item.state = "downloading"
        query = FakeQuery(f"cancel:{item.job_id}")
        context = make_context(bot_data={"downloads": {item.job_id: item}})

        await bot.handle_callback(make_update(query=query), context)

        self.assertTrue(item.cancel_requested.is_set())
        self.assertEqual(item.state, "downloading")
        self.assertIn("Cancelling", query.edits[-1][0])

    async def test_active_download_stops_through_pyrogram_progress_callback(self):
        item = make_job()
        client = CancellingClient(item)
        fake_bot = FakeBot()
        application = SimpleNamespace(
            bot=fake_bot,
            bot_data={"pyrogram_client": client},
        )

        await bot._process_queued_download(application, item)

        self.assertTrue(client.stop_called)
        self.assertEqual(item.state, "cancelled")
        self.assertIn("Cancelled", fake_bot.status_messages[0].edits[-1][0])

    async def test_successful_download_reports_text_progress_and_completion(self):
        item = make_job()
        client = SuccessfulClient()
        fake_bot = FakeBot()
        application = SimpleNamespace(
            bot=fake_bot,
            bot_data={"pyrogram_client": client},
        )

        await bot._process_queued_download(application, item)

        self.assertEqual(item.state, "completed")
        self.assertIsNotNone(fake_bot.sent[0]["reply_markup"])
        status_edits = fake_bot.status_messages[0].edits
        self.assertTrue(any("100%" in text for text, _ in status_edits))
        self.assertIn("Done", status_edits[-1][0])
        self.assertNotIn("█", "".join(text for text, _ in status_edits))

    async def test_worker_processes_queue_in_fifo_order(self):
        queue = asyncio.Queue()
        first = make_job("first.mkv")
        second = make_job("second.mkv")
        second.job_id = "job456"
        downloads = {first.job_id: first, second.job_id: second}
        application = SimpleNamespace(
            bot=FakeBot(),
            bot_data={
                "download_queue": queue,
                "downloads": downloads,
                "download_active": False,
            },
        )
        processed = []

        async def record(_application, item):
            processed.append(item.file_name)

        with patch.object(bot, "_process_queued_download", side_effect=record):
            worker = asyncio.create_task(bot._download_worker(application))
            await queue.put(first)
            await queue.put(second)
            await asyncio.wait_for(queue.join(), timeout=1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertEqual(processed, ["first.mkv", "second.mkv"])
        self.assertEqual(downloads, {})


def tearDownModule():
    shutil.rmtree(TEST_DIRECTORY, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
