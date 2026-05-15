import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs._extra_tools import make_extra_tools


class DummyAgent:
    task = "read a downloaded csv"
    _file_sandbox = ""
    _done_count_check_fired = False


class DummySession:
    def __init__(self, download_dir: str, download_path: str):
        self._download_dir = download_dir
        self._download_path = download_path

    async def download_dir(self) -> str:
        return self._download_dir

    async def list_downloads(self):
        return [
            (
                "guid-1",
                "ad_aqi_tracker_data.csv",
                "https://example.test/data.csv",
                "completed",
                12,
                12,
                self._download_path,
            )
        ]


class DownloadReadFileTests(unittest.TestCase):
    def _read_file_tool(self):
        for tool in make_extra_tools(DummyAgent()):
            if tool.name == "read_file":
                return tool
        raise AssertionError("read_file tool not found")

    def test_read_file_can_read_completed_download_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_path = Path(tmp) / "guid-download"
            download_path.write_text("date,aqi\n2026-05-14,58\n", encoding="utf-8")
            session = DummySession(tmp, str(download_path))
            read_file = self._read_file_tool()

            out = asyncio.run(
                read_file.func(session, "ad_aqi_tracker_data.csv")
            )

            self.assertIn("2026-05-14,58", out)

    def test_read_file_can_read_completed_download_by_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_path = Path(tmp) / "guid-download"
            download_path.write_text("downloaded content", encoding="utf-8")
            session = DummySession(tmp, str(download_path))
            read_file = self._read_file_tool()

            out = asyncio.run(read_file.func(session, str(download_path)))

            self.assertEqual(out, "downloaded content")

    def test_read_file_maps_reported_guid_path_to_suggested_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            reported_path = Path(tmp) / "guid-download"
            actual_path = Path(tmp) / "ad_aqi_tracker_data.csv"
            actual_path.write_text("date,aqi\n2026-05-14,50\n", encoding="utf-8")
            session = DummySession(tmp, str(reported_path))
            read_file = self._read_file_tool()

            out = asyncio.run(read_file.func(session, str(reported_path)))

            self.assertIn("2026-05-14,50", out)

    def test_read_file_rejects_unrelated_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_path = Path(tmp) / "guid-download"
            download_path.write_text("downloaded content", encoding="utf-8")
            with tempfile.NamedTemporaryFile("w", delete=False) as outside:
                outside.write("secret")
                outside_path = outside.name
            session = DummySession(tmp, str(download_path))
            read_file = self._read_file_tool()

            try:
                out = asyncio.run(read_file.func(session, outside_path))
            finally:
                Path(outside_path).unlink(missing_ok=True)

            self.assertEqual(out, f"(no such file: {outside_path})")


if __name__ == "__main__":
    unittest.main()
