"""Check GLB parsing, model choice inside ZIPs, and GLB files through the command line."""
import io
import json
import math
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

import render
from uploads import UploadWorkspace

TRIANGLE = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0)], dtype="<f4")
PROCESSED = {"version": "2.0", "generator": render.PROCESSOR_GENERATOR}


def png(size=(2, 1), colour=(255, 0, 0)):
    data = io.BytesIO()
    Image.new("RGB", size, colour).save(data, "PNG")
    return data.getvalue()


def pack_glb(gltf, binary=b""):
    """Pack a glTF dictionary and its binary buffer into a GLB file."""
    if binary:
        gltf = {**gltf, "buffers": [{"byteLength": len(binary)}]}
    text = json.dumps(gltf).encode()
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<I4s", len(text), b"JSON") + text
    if binary:
        chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def textured_triangles(texture_transform=None, **extra):
    """Two instances of a textured triangle: one under a rotated parent node, and one untransformed."""
    image = png()
    uvs = np.array([(0, 0), (65535, 0), (0, 65535)], dtype="<u2")  # normalized, so 0 to 1
    binary = TRIANGLE.tobytes() + uvs.tobytes() + np.array([0, 1, 2], dtype="<u2").tobytes() + b"\0\0" + image
    texture = {"index": 0}
    if texture_transform:
        texture["extensions"] = {"KHR_texture_transform": texture_transform}
    half = math.sqrt(0.5)
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 2]}],
        "nodes": [
            {"rotation": [0, 0, half, half], "children": [1]},  # 90° around Z
            {"translation": [1, 0, 0], "mesh": 0},
            {"mesh": 0},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": texture}}],
        "textures": [{"source": 0}],
        "images": [{"bufferView": 3, "mimeType": "image/png"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "normalized": True, "count": 3, "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123, "count": 3, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 36},
            {"buffer": 0, "byteOffset": 36, "byteLength": 12},
            {"buffer": 0, "byteOffset": 48, "byteLength": 6},
            {"buffer": 0, "byteOffset": 56, "byteLength": len(image)},
        ],
        **extra,
    }
    return pack_glb(gltf, binary), image


class GlbTests(unittest.TestCase):
    def test_node_transforms_texture_and_uvs(self):
        data, image = textured_triangles()
        [(vertices, texture, colour)] = render.load_glb(data)
        self.assertEqual(texture, image)
        self.assertEqual(colour, (1.0, 1.0, 1.0))
        # The rotated copy sits above and left of the plain one, and the pair is centred and scaled to fit 2 units
        np.testing.assert_allclose(vertices[:, :3], [
            (0, 0, 0), (0, 1, 0), (-1, 0, 0),
            (0, -1, 0), (1, -1, 0), (0, 0, 0),
        ], atol=1e-6)
        # glTF rows count from the top of the image, so v flips to match OBJ and OpenGL
        np.testing.assert_allclose(vertices[:, 3:], [(0, 1), (1, 1), (0, 0)] * 2, atol=1e-6)
        self.assertEqual(vertices.dtype, np.float32)

    def test_texture_transform(self):
        transform = {"offset": [0.5, 0.25], "rotation": math.pi / 2, "scale": [2, 3]}
        [(vertices, _, _)] = render.load_glb(textured_triangles(transform)[0])
        # (u, v) becomes (3v + 0.5, 0.25 - 2u), then v flips
        np.testing.assert_allclose(vertices[:3, 3:], [(0.5, 0.75), (0.5, 2.75), (3.5, 0.75)], atol=1e-6)

    def test_matrix_node_interleaved_attributes_and_flat_colour(self):
        uvs = np.array([(0.25, 0.5), (1, 0), (0, 1)], dtype="<f4")
        binary = np.hstack([TRIANGLE, uvs]).tobytes()  # one buffer view, 20 bytes per vertex
        gltf = {
            "asset": {"version": "2.0"},
            "nodes": [{"mesh": 0, "matrix": [0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1]}],  # 90° around Y
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "material": 0}]}],
            "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.25, 1, 1]}}],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
                {"bufferView": 0, "byteOffset": 12, "componentType": 5126, "count": 3, "type": "VEC2"},
            ],
            "bufferViews": [{"buffer": 0, "byteLength": 60, "byteStride": 20}],
        }
        [(vertices, texture, colour)] = render.load_glb(pack_glb(gltf, binary))
        self.assertIsNone(texture)
        np.testing.assert_allclose(colour, (0.5 ** (1 / 2.2), 0.25 ** (1 / 2.2), 1))
        np.testing.assert_allclose(vertices[:, :3], [(0, -1, 1), (0, -1, -1), (0, 1, 1)], atol=1e-6)
        np.testing.assert_allclose(vertices[:, 3:], [(0.25, 0.5), (1, 1), (0, 0)], atol=1e-6)

    def test_unreadable_files_explain_the_problem(self):
        compressed, _ = textured_triangles(extensionsRequired=["KHR_draco_mesh_compression"])
        lines = {
            "asset": {"version": "2.0"},
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "mode": 1}]}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
            "bufferViews": [{"buffer": 0, "byteLength": 36}],
        }
        for data, message in ((compressed, "Draco"), (pack_glb(lines, TRIANGLE.tobytes()), "no triangles"),
                              (b"PK\3\4not a GLB", "isn't a GLB")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                render.load_glb(data)


class SourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def zip_with(self, name, members):
        path = self.folder / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, data in members.items():
                archive.writestr(member, data)
        return path

    def test_zip_prefers_processed_glb_then_textured_obj_then_glb_then_bare_obj(self):
        obj = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
        glb = textured_triangles()[0]
        processed = textured_triangles(asset=PROCESSED)[0]
        cases = (
            # A Pedestal 3D ZIP after the GLB texture processor replaced bundle-medium.glb with a brightened copy
            ({"model.obj": "mtllib model.mtl\n" + obj * 200, "model.mtl": "newmtl a\n",
              "bundle-high.glb": glb + bytes(5000), "bundle-medium.glb": processed}, "bundle-medium.glb"),
            ({"small.obj": "mtllib small.mtl\n" + obj, "small.mtl": "newmtl a\n", "model.glb": glb}, "small.obj"),
            ({"large.obj": obj * 200, "model.glb": glb, "__MACOSX/._other.glb": glb}, "model.glb"),
            ({"only.obj": obj}, "only.obj"),
            ({"readme.txt": "no model"}, None),
        )
        for n, (members, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                self.assertEqual(render.find_model(self.zip_with(f"{n}.zip", members)), expected)

    def test_processed_glbs_are_marked_with_their_brightening(self):
        processed = textured_triangles(asset=PROCESSED)[0]
        source = self.zip_with("item.zip", {"model.obj": "v 0 0 0\n", "bundle-medium.glb": processed,
                                            "bundle-low.glb": textured_triangles()[0]})
        self.assertEqual(render.describe_model(source, "bundle-medium.glb"), "bundle-medium.glb (processed)")
        self.assertEqual(render.describe_model(source, "bundle-low.glb"), "bundle-low.glb")
        self.assertEqual(render.describe_model(source, "model.obj"), "model.obj")
        bare = self.folder / "Chair.glb"
        bare.write_bytes(processed)
        self.assertEqual(render.describe_model(bare, "Chair.glb"), "Chair.glb (processed)")
        for stops, label in ((2.0, "Chair.glb (processed, +2 stops)"), (1, "Chair.glb (processed, +1 stop)"),
                             (0.5, "Chair.glb (processed, +0.5 stops)"), (0, "Chair.glb (processed)")):
            with self.subTest(stops=stops):
                bare.write_bytes(textured_triangles(asset={**PROCESSED, "extras": {"brightenStops": stops}})[0])
                self.assertEqual(render.describe_model(bare, "Chair.glb"), label)
        for data in (b"", b"glTF" + bytes(16), processed[:30], b"PK\3\4" + bytes(40)):
            with self.subTest(data=data[:8]):
                self.assertIsNone(render.processor_stops(io.BytesIO(data)))

    def test_folders_include_glb_files_and_a_glb_is_its_own_model(self):
        for name in ("b.glb", "a.zip", "notes.txt", "C.GLB"):
            (self.folder / name).write_bytes(b"")
        self.assertEqual([path.name for path in render.find_sources([self.folder])], ["C.GLB", "a.zip", "b.glb"])
        self.assertEqual(render.find_model(self.folder / "b.glb"), "b.glb")

    def test_uploads_accept_glb_files(self):
        workspace = UploadWorkspace()
        self.addCleanup(workspace.directory.cleanup)
        glb = io.BytesIO(b"glTF")
        glb.name = "Chair.glb"
        self.assertEqual(workspace.save(glb).read_bytes(), b"glTF")
        other = io.BytesIO(b"solid")
        other.name = "Chair.stl"
        with self.assertRaisesRegex(ValueError, "ZIP or GLB"):
            workspace.save(other)

    def test_cli_renders_glb_file_and_leaves_it_alone_with_add_to_zip(self):
        source = self.folder / "Chair.glb"
        source.write_bytes(textured_triangles()[0])
        renderer = MagicMock()
        renderer.load.return_value = 2
        with patch.object(render.sys, "argv", ["render.py", "--add-to-zip", "--format", "both", str(source)]), \
                patch.object(render, "Renderer", return_value=renderer), \
                patch.object(render, "encode", side_effect=lambda _renderer, video: video.write_bytes(b"video")):
            render.main()
        renderer.load.assert_called_once_with(source, "Chair.glb")
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["Chair.glb", "Chair.mp4", "Chair.wmv"])
        self.assertEqual(source.read_bytes(), textured_triangles()[0])


@unittest.skipUnless(render.sys.platform.startswith("linux"), "Requires Linux EGL libraries")
class HeadlessGlbTests(unittest.TestCase):
    def test_texture_lands_the_right_way_up(self):
        with tempfile.TemporaryDirectory() as folder, patch.multiple(render, WIDTH=192, HEIGHT=108, FRAMES=4):
            texture = Image.new("RGB", (4, 4), (30, 30, 200))
            texture.paste((200, 30, 30), (0, 0, 4, 2))  # red top half, blue bottom half
            data = io.BytesIO()
            texture.save(data, "PNG")
            image = data.getvalue()
            binary = np.array([(-1, -1, 0), (1, -1, 0), (-1, 1, 0), (1, 1, 0)], dtype="<f4").tobytes() \
                + np.array([(0, 1), (1, 1), (0, 0), (1, 0)], dtype="<f4").tobytes() \
                + np.array([0, 1, 2, 2, 1, 3], dtype="<u2").tobytes() + image
            gltf = {
                "asset": {"version": "2.0"},
                "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2,
                                            "material": 0}]}],
                "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
                "textures": [{"source": 0}],
                "images": [{"bufferView": 3, "mimeType": "image/png"}],
                "accessors": [
                    {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
                    {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC2"},
                    {"bufferView": 2, "componentType": 5123, "count": 6, "type": "SCALAR"},
                ],
                "bufferViews": [
                    {"buffer": 0, "byteOffset": 0, "byteLength": 48},
                    {"buffer": 0, "byteOffset": 48, "byteLength": 32},
                    {"buffer": 0, "byteOffset": 80, "byteLength": 12},
                    {"buffer": 0, "byteOffset": 92, "byteLength": len(image)},
                ],
            }
            source = Path(folder) / "square.glb"
            source.write_bytes(pack_glb(gltf, binary))
            renderer = render.Renderer()
            try:
                self.assertEqual(renderer.load(source, render.find_model(source)), 2)
                frame = renderer.frame(0)
                # The square's top edge has v = 0, which glTF puts at the top of the image
                np.testing.assert_allclose(frame[34, 96], (200, 30, 30), atol=3)
                np.testing.assert_allclose(frame[74, 96], (30, 30, 200), atol=3)
            finally:
                renderer.release()


if __name__ == "__main__":
    unittest.main()
