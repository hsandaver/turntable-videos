"""Run with: python -m unittest discover -s outputs/tile11-sample -p 'test_*.py'."""
import io
import os
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from uploads import UploadWorkspace


def upload(name="Tile 11.zip", model=True):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("model.obj" if model else "readme.txt", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    data.name = name
    return data


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = UploadWorkspace()
        self.addCleanup(self.workspace.directory.cleanup)

    def test_rerun_preserves_video_added_to_zip(self):
        item = upload()
        path = self.workspace.save(item)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("Tile 11.wmv", b"video")
        self.assertEqual(path, self.workspace.save(item))
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read("Tile 11.wmv"), b"video")
        with zipfile.ZipFile(item) as archive:
            self.assertNotIn("Tile 11.wmv", archive.namelist())

    def test_same_names_and_separate_sessions_do_not_collide(self):
        first = self.workspace.save(upload())
        second = self.workspace.save(upload(model=False))
        self.assertNotEqual(first.parent, second.parent)
        other = UploadWorkspace()
        self.addCleanup(other.directory.cleanup)
        self.assertNotEqual(first, other.save(upload()))

    def test_client_paths_are_reduced_to_filenames(self):
        for name in ("../../Tile 11.zip", "C:\\models\\Tile 11.zip"):
            path = self.workspace.save(upload(name))
            self.assertEqual(path.name, "Tile 11.zip")
            self.assertTrue(path.is_relative_to(self.workspace.directory.name))


class AppTests(unittest.TestCase):
    def app(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=10)
        self.addCleanup(lambda: app.session_state["work"]["workspace"].directory.cleanup())
        return app

    def test_default_page_uses_browser_uploads_and_isolates_sessions(self):
        with patch.dict(os.environ, {"TURNTABLE_LOCAL_FILES": "0"}):
            first, second = self.app().run(), self.app().run()
        self.assertFalse(first.exception)
        self.assertEqual(len(first.get("file_uploader")), 1)
        self.assertNotIn("Choose ZIP files…", [button.label for button in first.button])
        self.assertIsNot(first.session_state["work"], second.session_state["work"])

    def test_uploaded_zip_renders_and_offers_video_and_zip_downloads(self):
        import render

        item = upload()
        renderer = MagicMock()

        def encode(_renderer, path, on_frame):
            path.write_bytes(b"test video")
            on_frame(render.FRAMES)

        with patch.dict(os.environ, {"TURNTABLE_LOCAL_FILES": "0"}), \
                patch("streamlit.file_uploader", return_value=[item]), \
                patch.object(render, "Renderer", return_value=renderer), \
                patch.object(render, "encode", side_effect=encode):
            app = self.app().run()
            self.assertFalse(app.exception)
            app.toggle[0].set_value(True).run()
            next(button for button in app.button if button.label == "Make 1 video").click().run()
            job = app.session_state["work"]["job"]
            deadline = time.monotonic() + 5
            while not job.finished and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(job.finished)
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(job.results[0][1], "Done")
            self.assertEqual(len(app.get("download_button")), 2)
            with zipfile.ZipFile(job.zips[0]) as archive:
                self.assertEqual(archive.read("Tile 11.wmv"), b"test video")
            app.run()
            self.assertEqual(len(app.get("download_button")), 2)

    def test_local_file_mode_remains_available_when_enabled(self):
        with patch.dict(os.environ, {"TURNTABLE_LOCAL_FILES": "1"}):
            app = self.app().run()
            app.radio[0].set_value("Local files").run()
        self.assertFalse(app.exception)
        self.assertIn("Choose ZIP files…", [button.label for button in app.button])


if __name__ == "__main__":
    unittest.main()
