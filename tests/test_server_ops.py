import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server_ops


class ServerOperationsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.media = root / "media"
        self.data = root / "data"
        self.media.mkdir()
        self.patches = [
            patch.object(server_ops, "MEDIA", self.media),
            patch.object(server_ops, "DATA", self.data),
            patch.object(server_ops, "QUEUE", self.data / "queue"),
            patch.object(server_ops, "DONE", self.data / "done"),
            patch.object(server_ops, "INCOMING", self.media / ".moviecatcher-incoming"),
            patch.object(server_ops, "BACKUP", self.media / ".moviecatcher-originals"),
        ]
        for item in self.patches:
            item.start()
        self.source = self.media / "series" / "episode.mkv"
        self.source.parent.mkdir()
        self.source.write_bytes(b"original-video")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def call(self, command, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            server_ops.run(command, list(arguments))
        return json.loads(output.getvalue())

    def complete(self, profile, mode):
        record = self.call("enqueue", "series/episode.mkv", profile, mode)
        incoming = server_ops.incoming_path(record["id"])
        incoming.parent.mkdir()
        payload = b"converted-video"
        incoming.write_bytes(payload)
        return record, payload

    def test_test_mode_keeps_original(self):
        record, payload = self.complete("passthrough", "test")
        result = self.call("complete", record["id"], hashlib.sha256(payload).hexdigest(), str(len(payload)))
        self.assertEqual(self.source.read_bytes(), b"original-video")
        self.assertEqual((self.media / result["output"]).read_bytes(), payload)
        self.assertNotIn("backup", result)

    def test_replace_mode_backs_up_original(self):
        record, payload = self.complete("hdr10-copy", "replace")
        result = self.call("complete", record["id"], hashlib.sha256(payload).hexdigest(), str(len(payload)))
        self.assertFalse(self.source.exists())
        self.assertEqual((self.media / result["backup"]).read_bytes(), b"original-video")
        self.assertEqual((self.media / result["output"]).read_bytes(), payload)

    def test_bad_hash_does_not_move_original(self):
        record, payload = self.complete("hdr10-copy", "replace")
        with self.assertRaises(ValueError):
            self.call("complete", record["id"], "0" * 64, str(len(payload)))
        self.assertEqual(self.source.read_bytes(), b"original-video")


if __name__ == "__main__":
    unittest.main()
