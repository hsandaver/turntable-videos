"""Render turntable videos (.wmv or .mp4) of models in Pedestal 3D ZIP downloads.

Each ZIP gets a 15-second 1920x1080 video of its model making one full turn.
WMV is the default for Acquia DAM previews; MP4 is also available. Videos take
the ZIP's name ("Tile 11.zip" becomes "Tile 11.wmv" or "Tile 11.mp4").

Works on Windows, macOS and Linux. Rendering uses OpenGL through moderngl, and
the encoder is the ffmpeg binary that ships inside imageio-ffmpeg.

    pip install -r requirements.txt
    python render.py "Tile 11.zip"                # writes "Tile 11.wmv" next to the ZIP
    python render.py path/to/folder               # every ZIP in the folder
    python render.py --format mp4 path/to/folder   # MP4 instead of WMV; use both for both formats
    python render.py --preview path/to/folder     # one still per ZIP (.png), to check before rendering
    python render.py --add-to-zip path/to/folder  # also put each video inside its ZIP
"""
import argparse
import io
import os
import posixpath
import shutil
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


def find_zips(paths):
    zips = []
    for path in map(Path, paths):
        if path.is_dir():
            zips += sorted(p for p in path.iterdir() if p.suffix.lower() == ".zip")
        else:
            zips.append(path)
    return zips


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


def pick_obj(zf):
    """Choose the model to render: the largest OBJ with a material file, or the largest OBJ if none has one.

    Pedestal 3D ZIPs often hold low, medium and high detail copies of the same model, and the largest is the most detailed.
    """
    def has_material(info):
        with io.TextIOWrapper(zf.open(info), encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                if line.startswith("mtllib"):
                    return resolve(zf, info.filename, line[len("mtllib"):]) is not None
        return False

    objs = [
        info for info in zf.infolist()
        if info.filename.lower().endswith(".obj")
        and not info.filename.startswith("__MACOSX/")
        and not posixpath.basename(info.filename).startswith("._")
    ]
    return max(objs, key=lambda info: (has_material(info), info.file_size), default=None)


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


def load_obj(zf, name):
    """Return {material name: float32 array of [x, y, z, u, v] per triangle corner} and the MTL data."""
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

    # Centre on the bounding box and scale the longest side to 2 units
    lo, hi = positions[1:].min(axis=0), positions[1:].max(axis=0)
    positions = ((positions - (lo + hi) / 2) * (2 / (hi - lo).max())).astype(np.float32)

    meshes = {}
    for material, (v_idx, t_idx) in faces.items():
        meshes[material] = np.hstack([positions[np.array(v_idx)], uvs[np.array(t_idx)]])
    return meshes, materials


def load_texture(ctx, zf, material):
    if material and material["map_Kd"]:
        image = Image.open(io.BytesIO(zf.read(material["map_Kd"]))).convert("RGB")
        limit = ctx.info["GL_MAX_TEXTURE_SIZE"]
        if max(image.size) > limit:
            image.thumbnail((limit, limit))
    else:
        kd = material["Kd"] if material else (1.0, 1.0, 1.0)
        image = Image.new("RGB", (1, 1), tuple(round(c * 255) for c in kd))
    # OBJ texture coordinates start at the bottom row, and so does OpenGL
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

    def load(self, zf, obj_name):
        meshes, materials = load_obj(zf, obj_name)
        lo = np.min([vertices[:, :3].min(axis=0) for vertices in meshes.values()], axis=0)
        hi = np.max([vertices[:, :3].max(axis=0) for vertices in meshes.values()], axis=0)
        self.bounds = np.array(list(product(*zip(lo, hi))))
        self.update_view()
        for material, vertices in meshes.items():
            buffer = self.ctx.buffer(vertices.tobytes())
            vao = self.ctx.vertex_array(self.program, [(buffer, "3f 2f", "in_position", "in_uv")])
            self.parts.append((buffer, vao, load_texture(self.ctx, zf, materials.get(material))))
        return sum(len(vertices) for vertices in meshes.values()) // 3

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


def process(renderer, zip_path, args):
    started = time.time()
    out_dir = Path(args.out) if args.out else zip_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    videos = [out_dir / f"{zip_path.stem}.{fmt}" for fmt in video_formats(args.format)]

    with zipfile.ZipFile(zip_path) as zf:
        if args.add_to_zip and not args.preview:
            for video in videos[:]:
                if video.name in zf.namelist():
                    print(f"  skipped: the ZIP already contains {video.name}")
                    videos.remove(video)
            if not videos:
                return
        obj = pick_obj(zf)
        if obj is None:
            print("  skipped: no .obj model in this ZIP")
            return
        triangles = renderer.load(zf, obj.filename)
    print(f"  model: {obj.filename} ({triangles:,} triangles)", flush=True)

    try:
        if args.preview:
            still = out_dir / f"{zip_path.stem}.png"
            Image.fromarray(renderer.frame(0)).save(still)
            print(f"  wrote {still}")
            return
        for video in videos:
            encode(renderer, video)
            print(f"  wrote {video} in {time.time() - started:.0f}s")
            if args.add_to_zip:
                add_to_zip(zip_path, video)
                print(f"  added {video.name} to {zip_path.name}")
    finally:
        renderer.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", metavar="ZIP_OR_FOLDER", help="ZIP files, or folders of ZIP files")
    parser.add_argument("--out", metavar="FOLDER", help="write output here instead of next to each ZIP")
    parser.add_argument("--format", choices=("wmv", "mp4", "both"), default="wmv",
                        help="video format, default: wmv; both creates WMV and MP4 files")
    parser.add_argument("--preview", action="store_true", help="write one still frame (.png) per ZIP instead of a video")
    parser.add_argument("--add-to-zip", action="store_true", help="add each finished video to the top level of its ZIP")
    args = parser.parse_args()

    zips = find_zips(args.paths)
    if not zips:
        sys.exit("No ZIP files found")

    renderer = Renderer()
    failed = []
    for n, zip_path in enumerate(zips, 1):
        print(f"[{n}/{len(zips)}] {zip_path.name}", flush=True)
        try:
            process(renderer, zip_path, args)
        except Exception as error:
            print(f"  failed: {error}")
            failed.append(zip_path.name)
    if failed:
        sys.exit(f"{len(failed)} of {len(zips)} failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
