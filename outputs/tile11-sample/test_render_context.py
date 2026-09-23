"""Check platform selection and exercise the real Mesa renderer on Linux."""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import render


class ContextTests(unittest.TestCase):
    def test_linux_uses_versioned_libraries_when_discovery_fails(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(render, "find_library", return_value=None), \
                patch.object(render.moderngl, "create_standalone_context") as create:
            render.create_context()
        create.assert_called_once_with(require=330, backend="egl",
                                       libgl="libGL.so.1", libegl="libEGL.so.1")

    def test_linux_uses_discovered_libraries(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(render, "find_library", side_effect=["/lib/libGL.so.1", "/lib/libEGL.so.1"]), \
                patch.object(render.moderngl, "create_standalone_context") as create:
            render.create_context()
        create.assert_called_once_with(require=330, backend="egl",
                                       libgl="/lib/libGL.so.1", libegl="/lib/libEGL.so.1")

    def test_other_platforms_use_native_backend(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(render.moderngl, "create_standalone_context") as create:
                render.create_context()
                create.assert_called_once_with(require=330)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux EGL libraries")
    def test_real_headless_render_and_video(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.multiple(render, WIDTH=192, HEIGHT=108, FRAMES=4):
            source = Path(folder) / "triangle.zip"
            video = source.with_suffix(".wmv")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("triangle.obj", "v -1 -1 0\nv 1 -1 0\nv 0 1 0\nf 1 2 3\n")
            renderer = render.Renderer()
            try:
                with zipfile.ZipFile(source) as archive:
                    self.assertEqual(renderer.load(archive, "triangle.obj"), 1)
                frame = renderer.frame(0)
                self.assertEqual(frame.shape, (108, 192, 3))
                self.assertTrue((frame != frame[0, 0]).any(), "Preview contains only the background")
                render.encode(renderer, video)
                self.assertGreater(video.stat().st_size, 0)
                render.add_to_zip(source, video)
                with zipfile.ZipFile(source) as archive:
                    self.assertEqual(archive.read(video.name), video.read_bytes())
            finally:
                renderer.release()


if __name__ == "__main__":
    unittest.main()
