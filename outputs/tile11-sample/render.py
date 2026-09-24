"""Render turntable videos (.wmv or .mp4) of models in Pedestal 3D ZIP downloads and GLB files.

Each ZIP or GLB file gets a 15-second 1920x1080 video of its model making one full turn.
WMV is the default for Acquia DAM previews; MP4 is also available. Videos take
the file's name ("Tile 11.zip" becomes "Tile 11.wmv" or "Tile 11.mp4").

Works on Windows, macOS and Linux. Rendering uses OpenGL through moderngl, and
the encoder is the ffmpeg binary that ships inside imageio-ffmpeg.

    pip install -r requirements.txt
    python render.py "Tile 11.zip"                # writes "Tile 11.wmv" next to the ZIP
    python render.py Chair.glb                    # writes "Chair.wmv" next to the GLB
    python render.py path/to/folder               # every ZIP and GLB file in the folder
    python render.py --format mp4 path/to/folder   # MP4 instead of WMV; use both for both formats
    python render.py --preview path/to/folder     # one still per file (.png), to check before rendering
    python render.py --add-to-zip path/to/folder  # also put each video inside its ZIP
"""
import argparse
import base64
import io
import json
import os
import posixpath
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from ctypes.util import find_library
from itertools import product
from pathlib import Path

import imageio_ffmpeg
import moderngl
import numpy as np
from PIL import Image

# Bump when app.py needs a new renderer interface, so a hot update can refresh a cached import.
RENDERER_API_VERSION = 4

WIDTH, HEIGHT = 1920, 1080
FPS = 30
FRAMES = 450  # one full turn over 15 seconds
BACKGROUND = (230 / 255, 230 / 255, 230 / 255)
CAMERA = (0.0, 0.3, 5.0)
ORTHO_HALF_HEIGHT = 1.5  # models are scaled so their longest side is 2, which fills two thirds of the frame height

VERTEX_SHADER = """
#version 330
uniform mat4 mvp;
in vec3 in_position;
in vec2 in_uv;
out vec2 uv;
void main() {
    gl_Position = mvp * vec4(in_position, 1.0);
    uv = in_uv;
}
"""

# Unlit: the scans already have lighting baked into their textures, so output the texture colour as is
FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
in vec2 uv;
out vec4 color;
void main() {
    color = vec4(texture(tex, uv).rgb, 1.0);
}
"""

# glTF accessor component types, and how many components each accessor type has
GLTF_COMPONENTS = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
GLTF_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
# Required glTF extensions that store the model in a form this script can't decode
GLTF_UNREADABLE = {
    "KHR_draco_mesh_compression": "Draco mesh compression",
    "EXT_meshopt_compression": "meshopt compression",
    "KHR_meshopt_compression": "meshopt compression",
    "KHR_texture_basisu": "KTX2 textures",
}
# The GLB texture processor app writes this to asset.generator in every GLB it makes, and its total brightening
# in stops to asset.extras.brightenStops
PROCESSOR_GENERATOR = "GLB texture processor"


def is_glb(path):
    return str(path).lower().endswith(".glb")


def processor_stops(f):
    """Read the GLB texture processor's record from the open GLB file `f`, reading only the glTF JSON.

    Returns None if the processor didn't make the GLB. Otherwise returns how many stops it brightened the textures,
    which is 0 if it didn't brighten them or made the GLB before it recorded brightening.
    """
    header = f.read(20)
    if len(header) < 20 or header[:4] != b"glTF" or header[16:20] != b"JSON":
        return None
    try:
        gltf = json.loads(f.read(struct.unpack_from("<I", header, 12)[0]))
    except ValueError:
        return None
    asset = gltf.get("asset") if isinstance(gltf, dict) else None
    if not isinstance(asset, dict) or asset.get("generator") != PROCESSOR_GENERATOR:
        return None
    extras = asset.get("extras")
    stops = extras.get("brightenStops", 0) if isinstance(extras, dict) else 0
    return stops if isinstance(stops, (int, float)) and not isinstance(stops, bool) else 0


def find_sources(paths):
    """Return the ZIP and GLB files in `paths`, looking inside any folders."""
    sources = []
    for path in map(Path, paths):
        if path.is_dir():
            sources += sorted(p for p in path.iterdir() if p.suffix.lower() in (".zip", ".glb"))
        else:
            sources.append(path)
    return sources


def resolve(zf, referrer, ref):
    """Return the ZIP member that `ref`, a path written inside the file `referrer`, points to."""
    ref = ref.strip().replace("\\", "/")
    names = zf.namelist()
    path = posixpath.normpath(posixpath.join(posixpath.dirname(referrer), ref))
    if path in names:
        return path
    # Some exporters write absolute paths from the machine that made the model, so fall back to the filename
    wanted = posixpath.basename(ref).lower()
    return next((n for n in names if posixpath.basename(n).lower() == wanted), None)


def pick_model(zf):
    """Choose the model to render, taking the largest of the first kind the ZIP has.

    In order: a GLB from the GLB texture processor, an OBJ with a material file, any other GLB, then any OBJ.
    A processed GLB is there to replace the original, for example with brightened textures, so it comes first.
    Pedestal 3D ZIPs often hold low, medium and high detail copies of the same model, and the largest is the most detailed.
    A GLB carries its own textures, so it beats an OBJ that has no material file to texture it.
    """
    def rank(info):
        if is_glb(info.filename):
            with zf.open(info) as f:
                return 3 if processor_stops(f) is not None else 1
        with io.TextIOWrapper(zf.open(info), encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                if line.startswith("mtllib"):
                    return 2 if resolve(zf, info.filename, line[len("mtllib"):]) is not None else 0
        return 0

    models = [
        info for info in zf.infolist()
        if info.filename.lower().endswith((".obj", ".glb"))
        and not info.filename.startswith("__MACOSX/")
        and not posixpath.basename(info.filename).startswith("._")
    ]
    return max(models, key=lambda info: (rank(info), info.file_size), default=None)


def find_model(path):
    """Return the name of the model to render in a ZIP, the file's own name for a GLB, or None if a ZIP has no model."""
    if is_glb(path):
        return Path(path).name
    with zipfile.ZipFile(path) as zf:
        info = pick_model(zf)
        return info.filename if info else None


def describe_model(path, model):
    """Return the model's name for display, marking a GLB from the GLB texture processor and its brightening."""
    if not is_glb(model):
        return model
    if is_glb(path):
        with open(path, "rb") as f:
            stops = processor_stops(f)
    else:
        with zipfile.ZipFile(path) as zf, zf.open(model) as f:
            stops = processor_stops(f)
    if stops is None:
        return model
    return f"{model} (processed, {stops:+g} stop{'' if stops == 1 else 's'})" if stops else f"{model} (processed)"


def load_mtl(zf, name):
    materials, current = {}, None
    for line in zf.read(name).decode("utf-8", errors="replace").splitlines():
        key, _, rest = line.strip().partition(" ")
        rest = rest.strip()
        if key == "newmtl":
            current = materials[rest] = {"Kd": (1.0, 1.0, 1.0), "map_Kd": None}
        elif current is not None and key == "Kd":
            current["Kd"] = tuple(float(x) for x in rest.split()[:3])
        elif current is not None and key == "map_Kd":
            current["map_Kd"] = resolve(zf, name, rest)
            if current["map_Kd"] is None:
                print(f"  warning: texture {rest} is not in the ZIP")
    return materials


def fit(positions, lo, hi):
    """Centre positions on the bounding box from `lo` to `hi` and scale its longest side to 2 units."""
    return ((positions - (lo + hi) / 2) * (2 / (hi - lo).max())).astype(np.float32)


def load_obj(zf, name):
    """Return a (vertices, image, colour) tuple per material.

    `vertices` is a float32 array of [x, y, z, u, v] per triangle corner, `image` is the encoded texture or None,
    and `colour` is the flat RGB colour to use when there's no texture.
    """
    # OBJ indices start at 1, so a dummy row at 0 lets them index the arrays directly.
    # A face corner with no texture coordinate also lands on uv row 0.
    positions, uvs = [("0", "0", "0")], [("0", "0")]
    faces = {}
    material, materials = None, {}
    with io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                positions.append(line.split()[1:4])  # any trailing r g b vertex colour is ignored
            elif line.startswith("vt "):
                uvs.append(line.split()[1:3])
            elif line.startswith("f "):
                v_idx, t_idx = faces.setdefault(material, ([], []))
                corners = []
                for token in line.split()[1:]:
                    parts = token.split("/")
                    v = int(parts[0])
                    t = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    corners.append((v if v > 0 else len(positions) + v, t if t >= 0 else len(uvs) + t))
                for i in range(1, len(corners) - 1):  # fan-triangulate quads and n-gons
                    for v, t in (corners[0], corners[i], corners[i + 1]):
                        v_idx.append(v)
                        t_idx.append(t)
            elif line.startswith("usemtl"):
                material = line[len("usemtl"):].strip()
            elif line.startswith("mtllib"):
                mtl = resolve(zf, name, line[len("mtllib"):])
                if mtl:
                    materials = load_mtl(zf, mtl)
                else:
                    print(f"  warning: {line.strip()} is not in the ZIP, so the model will render untextured")

    positions = np.array(positions, dtype=np.float64)
    uvs = np.array(uvs, dtype=np.float32)

    positions = fit(positions, positions[1:].min(axis=0), positions[1:].max(axis=0))

    meshes = []
    for material, (v_idx, t_idx) in faces.items():
        properties = materials.get(material) or {"Kd": (1.0, 1.0, 1.0), "map_Kd": None}
        image = zf.read(properties["map_Kd"]) if properties["map_Kd"] else None
        meshes.append((np.hstack([positions[np.array(v_idx)], uvs[np.array(t_idx)]]), image, properties["Kd"]))
    return meshes


def read_glb(data):
    """Split a GLB file into its glTF JSON and its binary chunk."""
    data = memoryview(data)
    if data[:4] != b"glTF":
        raise ValueError("This isn't a GLB file")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError(f"This is a version {version} GLB file, and only version 2 is supported")
    gltf, binary = None, None
    offset = 12
    while offset + 8 <= min(length, len(data)):
        size, kind = struct.unpack_from("<I4s", data, offset)
        if kind == b"JSON" and gltf is None:
            gltf = json.loads(bytes(data[offset + 8:offset + 8 + size]))
        elif kind == b"BIN\0" and binary is None:
            binary = data[offset + 8:offset + 8 + size]
        offset += 8 + size
    if gltf is None:
        raise ValueError("The GLB file has no glTF data in it")
    return gltf, binary


def node_matrix(node):
    """Return a glTF node's transform relative to its parent."""
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # glTF lists it column by column
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    matrix = np.identity(4)
    matrix[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    matrix[:3, :3] *= node.get("scale", (1, 1, 1))  # scaling the columns applies the scale before the rotation
    matrix[:3, 3] = node.get("translation", (0, 0, 0))
    return matrix


def load_glb(data):
    """Read a binary glTF model into the same (vertices, image, colour) tuples that load_obj returns."""
    gltf, binary = read_glb(data)
    for extension in gltf.get("extensionsRequired", []):
        if extension in GLTF_UNREADABLE:
            raise ValueError(f"The model uses {GLTF_UNREADABLE[extension]}, which isn't supported. "
                             "Export it again without it.")

    def embedded(uri):
        if uri.startswith("data:"):
            return base64.b64decode(uri.partition(",")[2])
        raise ValueError(f"The GLB file needs a separate file, {uri}. Export it again with everything embedded.")

    # A buffer with no URI is the GLB's own binary chunk
    buffers = [binary if "uri" not in buffer else embedded(buffer["uri"]) for buffer in gltf.get("buffers", [])]

    def view(index):
        spec = gltf["bufferViews"][index]
        start = spec.get("byteOffset", 0)
        return buffers[spec["buffer"]][start:start + spec["byteLength"]], spec.get("byteStride")

    def accessor(index):
        """Return an accessor's values as an array with one row per element. Normalized integers become floats."""
        spec = gltf["accessors"][index]
        if "sparse" in spec:
            raise ValueError("The model uses sparse accessors, which aren't supported. Export it again without them.")
        dtype, width = np.dtype(GLTF_COMPONENTS[spec["componentType"]]), GLTF_WIDTHS[spec["type"]]
        if "bufferView" not in spec:
            return np.zeros((spec["count"], width))
        data, stride = view(spec["bufferView"])
        values = np.ndarray((spec["count"], width), dtype, buffer=data, offset=spec.get("byteOffset", 0),
                            strides=(stride or dtype.itemsize * width, dtype.itemsize))
        if spec.get("normalized"):
            return np.maximum(values / np.iinfo(dtype).max, -1.0)
        return values.copy()

    def base_colour(material):
        """Return a material's baseColorTexture entry and its baseColorFactor."""
        pbr = gltf["materials"][material].get("pbrMetallicRoughness", {}) if material is not None else {}
        return pbr.get("baseColorTexture", {}), pbr.get("baseColorFactor", (1, 1, 1, 1))

    def texture(material):
        """Return a material's encoded texture image or None, and its flat colour for when there's no texture."""
        info, factor = base_colour(material)
        colour = tuple(c ** (1 / 2.2) for c in factor[:3])  # glTF colour factors are linear, but the video is sRGB
        if "index" not in info:
            return None, colour
        spec = gltf["textures"][info["index"]]
        source = spec.get("source", spec.get("extensions", {}).get("EXT_texture_webp", {}).get("source"))
        if source is None:
            print("  warning: a texture is in a format that isn't supported, so part of the model will render untextured")
            return None, colour
        image = gltf["images"][source]
        return (bytes(view(image["bufferView"])[0]) if "bufferView" in image else embedded(image["uri"])), colour

    nodes = gltf.get("nodes", [])
    if gltf.get("scenes"):
        roots = gltf["scenes"][gltf.get("scene", 0)].get("nodes", [])
    else:
        children = {child for node in nodes for child in node.get("children", [])}
        roots = [index for index in range(len(nodes)) if index not in children]

    primitives = []  # (material, world positions, texture coordinates, triangle corner indices)
    stack = [(index, np.identity(4)) for index in reversed(roots)]
    while stack:
        index, parent = stack.pop()
        node = nodes[index]
        world = parent @ node_matrix(node)
        stack += [(child, world) for child in reversed(node.get("children", []))]
        if "mesh" not in node:
            continue
        for primitive in gltf["meshes"][node["mesh"]]["primitives"]:
            if primitive.get("mode", 4) != 4:
                print("  warning: skipped a part of the model stored as points, lines, or triangle strips or fans")
                continue
            attributes, material = primitive["attributes"], primitive.get("material")
            positions = accessor(attributes["POSITION"]) @ world[:3, :3].T + world[:3, 3]
            info, _ = base_colour(material)
            transform = info.get("extensions", {}).get("KHR_texture_transform", {})
            uv_set = f"TEXCOORD_{transform.get('texCoord', info.get('texCoord', 0))}"
            uvs = accessor(attributes[uv_set]) if uv_set in attributes else np.zeros((len(positions), 2))
            if transform:
                # Scale, then rotate, then offset. The rotation direction follows the Khronos sample renderer
                # and three.js, which disagree with the GLSL in the KHR_texture_transform README.
                (u_offset, v_offset), (u_scale, v_scale) = transform.get("offset", (0, 0)), transform.get("scale", (1, 1))
                c, s = np.cos(transform.get("rotation", 0)), np.sin(transform.get("rotation", 0))
                u, v = uvs[:, 0] * u_scale, uvs[:, 1] * v_scale
                uvs = np.column_stack([c * u + s * v + u_offset, c * v - s * u + v_offset])
            uvs[:, 1] = 1 - uvs[:, 1]  # glTF counts texture rows from the top, and OBJ from the bottom
            indices = accessor(primitive["indices"])[:, 0].astype(np.intp) if "indices" in primitive \
                else np.arange(len(positions))
            primitives.append((material, positions, uvs.astype(np.float32), indices))
    if not primitives:
        raise ValueError("The GLB file has no triangles to draw")

    lo = np.min([positions.min(axis=0) for _, positions, _, _ in primitives], axis=0)
    hi = np.max([positions.max(axis=0) for _, positions, _, _ in primitives], axis=0)
    meshes = {}
    for material, positions, uvs, indices in primitives:
        meshes.setdefault(material, []).append(np.hstack([fit(positions, lo, hi)[indices], uvs[indices]]))
    return [(np.concatenate(parts), *texture(material)) for material, parts in meshes.items()]


def load_texture(ctx, image, colour):
    """Make a texture from an encoded image, or a 1x1 texture of `colour` when there's no image."""
    if image is not None:
        image = Image.open(io.BytesIO(image)).convert("RGB")
        limit = ctx.info["GL_MAX_TEXTURE_SIZE"]
        if max(image.size) > limit:
            image.thumbnail((limit, limit))
    else:
        image = Image.new("RGB", (1, 1), tuple(round(c * 255) for c in colour))
    # OBJ texture coordinates start at the bottom row, and so does OpenGL. load_glb flips GLB ones to match.
    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    texture = ctx.texture(image.size, 3, image.tobytes())
    texture.build_mipmaps()
    texture.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
    texture.anisotropy = 16.0
    return texture


def look_at(eye, target, up=(0.0, 1.0, 0.0)):
    eye, target, up = (np.array(a, dtype=np.float64) for a in (eye, target, up))
    forward = target - eye
    forward /= np.linalg.norm(forward)
    side = np.cross(forward, up)
    side /= np.linalg.norm(side)
    up = np.cross(side, forward)
    view = np.identity(4)
    view[0, :3], view[1, :3], view[2, :3] = side, up, -forward
    view[:3, 3] = -view[:3, :3] @ eye
    return view


def orthographic(half_height, aspect, near=0.1, far=100.0):
    proj = np.identity(4)
    proj[0, 0] = 1 / (half_height * aspect)
    proj[1, 1] = 1 / half_height
    proj[2, 2] = -2 / (far - near)
    proj[2, 3] = -(far + near) / (far - near)
    return proj


def rotation_y(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]])


def orientation_matrix(angles=(0, 0, 0)):
    """Orient the model before the turntable spins it: X tilt, Z roll, then Y facing, in degrees."""
    x, y, z = np.radians(angles)
    cx, sx, cz, sz = np.cos(x), np.sin(x), np.cos(z), np.sin(z)
    tilt = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]])
    roll = np.array([[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    return rotation_y(y) @ roll @ tilt


def create_context():
    if sys.platform.startswith("linux"):
        # EGL works without a display server. Runtime packages provide the .so.1
        # libraries; the unversioned .so links require development packages.
        return moderngl.create_standalone_context(
            require=330, backend="egl",
            libgl=find_library("GL") or "libGL.so.1",
            libegl=find_library("EGL") or "libEGL.so.1",
        )
    return moderngl.create_standalone_context(require=330)


class Renderer:
    """One OpenGL context, reused for every ZIP in a run."""

    def __init__(self, width=None, height=None):
        self.width, self.height = width or WIDTH, height or HEIGHT
        self.orientation = np.identity(4)
        self.bounds = None
        self.ctx = create_context()
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        samples = min(4, self.ctx.max_samples)
        self.msaa = self.ctx.framebuffer(
            color_attachments=[self.ctx.renderbuffer((self.width, self.height), 4, samples=samples)],
            depth_attachment=self.ctx.depth_renderbuffer((self.width, self.height), samples=samples),
        )
        self.resolved = self.ctx.framebuffer(color_attachments=[self.ctx.renderbuffer((self.width, self.height), 4)])
        self.view_proj = orthographic(ORTHO_HALF_HEIGHT, self.width / self.height) @ look_at(CAMERA, (0, 0, 0))
        self.parts = []

    def set_orientation(self, angles=(0, 0, 0)):
        self.orientation = orientation_matrix(angles)
        self.update_view()

    def update_view(self):
        # Fit the entire turn, including tilted corners, in the centre preview square.
        half_height = ORTHO_HALF_HEIGHT
        if self.bounds is not None:
            points = self.bounds @ self.orientation[:3, :3].T
            radius = np.linalg.norm(points[:, [0, 2]], axis=1)
            elevation = np.arctan2(CAMERA[1], np.hypot(CAMERA[0], CAMERA[2]))
            vertical = np.abs(points[:, 1] * np.cos(elevation)) + radius * abs(np.sin(elevation))
            half_height = max(half_height, float(max(radius.max(), vertical.max())) / 0.95)
        self.view_proj = orthographic(half_height, self.width / self.height) @ look_at(CAMERA, (0, 0, 0))

    def load(self, path, model):
        """Load `model`, as named by find_model, from the ZIP or GLB file at `path`. Returns the triangle count."""
        if is_glb(path):
            meshes = load_glb(Path(path).read_bytes())
        else:
            with zipfile.ZipFile(path) as zf:
                meshes = load_glb(zf.read(model)) if is_glb(model) else load_obj(zf, model)
        lo = np.min([vertices[:, :3].min(axis=0) for vertices, _, _ in meshes], axis=0)
        hi = np.max([vertices[:, :3].max(axis=0) for vertices, _, _ in meshes], axis=0)
        self.bounds = np.array(list(product(*zip(lo, hi))))
        self.update_view()
        for vertices, image, colour in meshes:
            buffer = self.ctx.buffer(vertices.tobytes())
            vao = self.ctx.vertex_array(self.program, [(buffer, "3f 2f", "in_position", "in_uv")])
            self.parts.append((buffer, vao, load_texture(self.ctx, image, colour)))
        return sum(len(vertices) for vertices, _, _ in meshes) // 3

    def unload(self):
        for resources in self.parts:
            for resource in resources:
                resource.release()
        self.parts = []
        self.bounds = None

    def release(self):
        self.unload()
        self.ctx.release()

    def frame(self, i):
        mvp = self.view_proj @ rotation_y(i * 2 * np.pi / FRAMES) @ self.orientation
        self.program["mvp"].write(mvp.T.astype("f4").tobytes())  # GLSL wants column-major order
        self.msaa.use()
        self.msaa.clear(*BACKGROUND, 1.0)
        self.ctx.enable(moderngl.DEPTH_TEST)  # no face culling, so the inside of open scans still draws
        for _, vao, texture in self.parts:
            texture.use(0)
            vao.render()
        self.ctx.copy_framebuffer(self.resolved, self.msaa)
        pixels = np.frombuffer(self.resolved.read(components=3, alignment=1), dtype=np.uint8)
        return np.ascontiguousarray(pixels.reshape(self.height, self.width, 3)[::-1])  # OpenGL rows run bottom to top


def encode(renderer, video, on_frame=None):
    """Render every frame into `video`. `on_frame(frames_done)` runs after each frame, and may raise to stop early.

    The video is written under a temporary name and renamed at the end, so stopping early never leaves a
    half-written video behind or destroys one from an earlier run.
    """
    if video.suffix.lower() == ".wmv":
        output_args = ["-c:v", "wmv2", "-b:v", "12M", "-pix_fmt", "yuv420p", "-f", "asf"]
    elif video.suffix.lower() == ".mp4":
        output_args = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                       "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4"]
    else:
        raise ValueError(f"Unsupported video format: {video.suffix}")
    partial = video.with_name(video.name + ".partial")
    encoder = subprocess.Popen(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
            *output_args, str(partial),
        ],
        stdin=subprocess.PIPE,
    )
    finished = False
    try:
        for i in range(FRAMES):
            encoder.stdin.write(renderer.frame(i).tobytes())
            if on_frame:
                on_frame(i + 1)
        finished = True
    finally:
        encoder.stdin.close()
        # Wait before cleaning up, because Windows won't delete a file that ffmpeg still has open
        returncode = encoder.wait()
        if not finished or returncode != 0:
            partial.unlink(missing_ok=True)
    if returncode != 0:
        raise RuntimeError("ffmpeg could not encode the video")
    os.replace(partial, video)


def add_to_zip(zip_path, video):
    # Append to a copy and swap it in, so an interrupted run can't leave a damaged ZIP behind
    partial = zip_path.with_name(zip_path.name + ".partial")
    shutil.copyfile(zip_path, partial)
    with zipfile.ZipFile(partial, "a") as zf:
        zf.write(video, video.name, compress_type=zipfile.ZIP_STORED)  # Video is already compressed
    os.replace(partial, zip_path)


def video_formats(choice):
    return ("wmv", "mp4") if choice == "both" else (choice,)


def process(renderer, source, args):
    started = time.time()
    out_dir = Path(args.out) if args.out else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    videos = [out_dir / f"{source.stem}.{fmt}" for fmt in video_formats(args.format)]
    into_zip = args.add_to_zip and not is_glb(source)  # a GLB file on its own has no ZIP to add to

    if into_zip and not args.preview:
        with zipfile.ZipFile(source) as zf:
            for video in videos[:]:
                if video.name in zf.namelist():
                    print(f"  skipped: the ZIP already contains {video.name}")
                    videos.remove(video)
        if not videos:
            return
    model = find_model(source)
    if model is None:
        print("  skipped: no .obj or .glb model in this ZIP")
        return

    try:
        triangles = renderer.load(source, model)
        print(f"  model: {describe_model(source, model)} ({triangles:,} triangles)", flush=True)
        if args.preview:
            still = out_dir / f"{source.stem}.png"
            Image.fromarray(renderer.frame(0)).save(still)
            print(f"  wrote {still}")
            return
        for video in videos:
            encode(renderer, video)
            print(f"  wrote {video} in {time.time() - started:.0f}s")
            if into_zip:
                add_to_zip(source, video)
                print(f"  added {video.name} to {source.name}")
    finally:
        renderer.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", metavar="FILE_OR_FOLDER", help="ZIP or GLB files, or folders of them")
    parser.add_argument("--out", metavar="FOLDER", help="write output here instead of next to each ZIP or GLB file")
    parser.add_argument("--format", choices=("wmv", "mp4", "both"), default="wmv",
                        help="video format, default: wmv; both creates WMV and MP4 files")
    parser.add_argument("--preview", action="store_true", help="write one still frame (.png) per file instead of a video")
    parser.add_argument("--add-to-zip", action="store_true",
                        help="add each finished video to the top level of its ZIP; GLB files are left as they are")
    args = parser.parse_args()

    sources = find_sources(args.paths)
    if not sources:
        sys.exit("No ZIP or GLB files found")

    renderer = Renderer()
    failed = []
    for n, source in enumerate(sources, 1):
        print(f"[{n}/{len(sources)}] {source.name}", flush=True)
        try:
            process(renderer, source, args)
        except Exception as error:
            print(f"  failed: {error}")
            failed.append(source.name)
    if failed:
        sys.exit(f"{len(failed)} of {len(sources)} failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
