"""Exercise real video encoding, cancellation, and command-line format selection."""
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import render


class FormatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.renderer = MagicMock()
        self.renderer.frame.return_value = np.full((108, 192, 3), 180, dtype=np.uint8)
        dimensions = patch.multiple(render, WIDTH=192, HEIGHT=108, FRAMES=4)
        dimensions.start()
        self.addCleanup(dimensions.stop)

    def test_real_encoding_and_decoding_for_each_format(self):
        for fmt, codec in (("wmv", "wmv2"), ("mp4", "h264")):
            with self.subTest(fmt=fmt):
                video = self.folder / f"test.{fmt}"
                frames = []
                render.encode(self.renderer, video, frames.append)
                self.assertEqual(frames, [1, 2, 3, 4])
                result = subprocess.run(
                    [render.imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(video), "-f", "null", "-"],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Video: {codec}", result.stderr)
                self.assertIn("yuv420p", result.stderr)
                self.assertIn("192x108", result.stderr)
                if fmt == "mp4":
                    data = video.read_bytes()
                    self.assertIn(b"ftyp", data[:32])
                    self.assertGreater(data.find(b"moov"), 0)
                    self.assertLess(data.find(b"moov"), data.find(b"mdat"))
                self.assertFalse(video.with_name(video.name + ".partial").exists())

    def test_cancellation_preserves_previous_video(self):
        def cancel(_):
            raise InterruptedError("Cancelled")

        for fmt in ("wmv", "mp4"):
            with self.subTest(fmt=fmt):
                video = self.folder / f"test.{fmt}"
                video.write_bytes(b"previous video")
                with self.assertRaises(InterruptedError):
                    render.encode(self.renderer, video, cancel)
                self.assertEqual(video.read_bytes(), b"previous video")
                self.assertFalse(video.with_name(video.name + ".partial").exists())

    def test_cli_formats_and_zip_insertion(self):
        for choice, formats in ((None, ("wmv",)), ("mp4", ("mp4",)),
                                ("both", ("wmv", "mp4"))):
            with self.subTest(choice=choice):
                source = self.folder / f"{choice}.zip"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("model.obj", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
                    if choice == "both":
                        archive.writestr(f"{source.stem}.wmv", b"existing WMV")
                argv = ["render.py", "--add-to-zip", str(source)]
                if choice:
                    argv += ["--format", choice]
                self.renderer.load.return_value = 1
                with patch.object(render.sys, "argv", argv), \
                        patch.object(render, "Renderer", return_value=self.renderer):
                    render.main()
                with zipfile.ZipFile(source) as archive:
                    for fmt in formats:
                        name = f"{source.stem}.{fmt}"
                        self.assertEqual(archive.namelist().count(name), 1)
                        if choice == "both" and fmt == "wmv":
                            self.assertEqual(archive.read(name), b"existing WMV")
                        else:
                            self.assertEqual(archive.read(name), (self.folder / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
