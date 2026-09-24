"""Streamlit app for making turntable videos from Pedestal 3D ZIP downloads and GLB files.

    pip install -r requirements.txt
    streamlit run app.py

The rendering itself lives in render.py, which also works on its own from the command line.
"""
import importlib
import os
import sys
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import file_dialog  # noqa: E402
import render  # noqa: E402
from uploads import UploadWorkspace  # noqa: E402

# A hosted hot update can rerun this file with the previous render module still imported.
# Refresh only an incompatible interface, not on every rerun while jobs are working.
if getattr(render, "RENDERER_API_VERSION", None) != 4:
    importlib.reload(render)

PREVIEW_SIZE = 360  # pixels per view in the preview strips
PREVIEW_FRAMES = 36
LOCAL_FILES = os.environ.get("TURNTABLE_LOCAL_FILES") == "1"


class Cancelled(Exception):
    pass


@dataclass
class Job:
    """A preview or render run. A background thread fills it in while the page polls it."""

    kind: str  # "preview" or "render"
    sources: list  # ZIP and GLB file paths
    out_dir: Path | None = None
    add_to_zip: bool = False
    formats: tuple = ("wmv",)
    workspace: UploadWorkspace | None = None  # keep temporary files alive during background work
    orientations: dict = field(default_factory=dict)  # Path -> (X tilt, Y facing, Z roll), degrees
    downloads: list = field(default_factory=list)
    done: int = 0
    current: str = ""
    stage: str = ""
    frame: int = 0
    total_frames: int = render.FRAMES
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    results: list = field(default_factory=list)  # (file name, outcome, detail)
    previews: list = field(default_factory=list)  # (source path, model, triangles, JPEG bytes, GIF bytes)
    error: str = ""
    finished: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)


def session():
    """Keep jobs and uploads private to this browser session."""
    if "work" not in st.session_state:
        st.session_state.work = {"job": None, "workspace": UploadWorkspace(), "orientations": {}}
    st.session_state.work.setdefault("orientations", {})
    return st.session_state.work


# Background work. Nothing here calls st.*, because Streamlit commands only work on the page's own thread.

def run_job(job):
    try:
        # The OpenGL context has to be created on the thread that uses it
        renderer = render.Renderer(width=640, height=360) if job.kind == "preview" else render.Renderer()
    except Exception as error:
        job.error = f"Could not start renderer: {error}"
        job.ended, job.finished = time.time(), True
        return
    try:
        for path in job.sources:
            if job.cancel.is_set():
                break
            job.current, job.stage, job.frame = path.name, "Loading model", 0
            try:
                renderer.set_orientation(job.orientations.get(path, (0, 0, 0)))
                (preview_source if job.kind == "preview" else render_source)(renderer, path, job)
            except Cancelled:
                job.results.append((path.name, "Cancelled", "Stopped before the preview or video was finished"))
            except Exception as error:
                job.results.append((path.name, "Failed", str(error)))
            finally:
                renderer.unload()
                job.done += 1
    finally:
        renderer.release()
        job.ended, job.finished = time.time(), True


def preview_source(renderer, path, job):
    model = render.find_model(path)
    if model is None:
        job.results.append((path.name, "Skipped", "No .obj or .glb model in this ZIP"))
        return
    triangles = renderer.load(path, model)
    model = render.describe_model(path, model)

    job.stage = "Rendering preview"
    job.total_frames = PREVIEW_FRAMES
    frames = []
    for k in range(PREVIEW_FRAMES):
        if job.cancel.is_set():
            raise Cancelled
        frames.append(Image.fromarray(renderer.frame(k * render.FRAMES / PREVIEW_FRAMES)))
        job.frame = k + 1
    left = (renderer.width - renderer.height) // 2
    strip = Image.new("RGB", (PREVIEW_SIZE * 4, PREVIEW_SIZE))
    for k in range(4):
        view = frames[k * PREVIEW_FRAMES // 4]
        view = view.crop((left, 0, left + renderer.height, renderer.height))
        strip.paste(view.resize((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS), (k * PREVIEW_SIZE, 0))
    jpeg = BytesIO()
    strip.save(jpeg, "JPEG", quality=88)
    animation = BytesIO()
    frames[0].save(animation, "GIF", save_all=True, append_images=frames[1:],
                   duration=120, loop=0)
    job.previews.append((path, model, triangles, jpeg.getvalue(), animation.getvalue()))
    job.results.append((path.name, "Previewed", model))


def render_source(renderer, path, job):
    out_dir = job.out_dir or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    videos = [out_dir / f"{path.stem}.{fmt}" for fmt in job.formats]
    into_zip = job.add_to_zip and not render.is_glb(path)  # a GLB file on its own has no ZIP to add to

    if into_zip:
        with zipfile.ZipFile(path) as zf:
            videos = [video for video in videos if video.name not in zf.namelist()]
        if not videos:
            job.results.append((path.name, "Skipped", "The ZIP already contains the selected formats"))
            return
    model = render.find_model(path)
    if model is None:
        job.results.append((path.name, "Skipped", "No .obj or .glb model in this ZIP"))
        return
    renderer.load(path, model)

    job.total_frames = len(videos) * render.FRAMES

    def on_frame(frames_done):
        job.frame = index * render.FRAMES + frames_done
        if job.cancel.is_set():
            raise Cancelled

    details = []
    for index, video in enumerate(videos):
        if job.cancel.is_set():
            raise Cancelled
        job.stage = f"Rendering {video.suffix[1:].upper()}"
        render.encode(renderer, video, on_frame)
        saved = f"Ready to download: {video.name}" if job.workspace else (
            f"Saved {video.name} " + (f"next to {path.name}" if job.out_dir is None else f"in {job.out_dir}")
        )
        job.downloads.append(video)
        if into_zip:
            job.stage = "Adding the video to the ZIP"
            render.add_to_zip(path, video)
            if path not in job.downloads:
                job.downloads.append(path)
            saved += " and added it to the ZIP"
        details.append(saved)
    job.results.append((path.name, "Done", "; ".join(details)))


def start(job):
    session()["job"] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    st.rerun()


# Folder scanning

@st.cache_data(show_spinner=False)
def inspect_source(path, modified, size, formats):
    """Describe the model to render and list the formats inside the ZIP. `modified` and `size` bust the cache."""
    try:
        if render.is_glb(path):
            return render.describe_model(path, Path(path).name), set()
        with zipfile.ZipFile(path) as zf:
            info = render.pick_model(zf)
            inside = {fmt for fmt in formats if f"{Path(path).stem}.{fmt}" in zf.namelist()}
        return (render.describe_model(path, info.filename) if info else None), inside
    except (zipfile.BadZipFile, OSError):
        return None, set()


def scan(source, out_dir, add_to_zip, formats):
    rows, without_model = [], 0
    for path in render.find_sources(source):
        info = path.stat()
        model, inside = inspect_source(str(path), info.st_mtime, info.st_size, formats)
        if model is None:
            without_model += 1
            continue
        statuses = {}
        for fmt in formats:
            saved = ((out_dir or path.parent) / f"{path.stem}.{fmt}").exists()
            statuses[fmt] = "In ZIP" if fmt in inside else "Saved" if saved else "Not made"
        status = ", ".join(f"{fmt.upper()}: {value}" for fmt, value in statuses.items())
        into_zip = add_to_zip and not render.is_glb(path)
        rows.append({
            "path": path,
            "File": path.name,
            "Folder": str(path.parent),
            "Model": model,
            "Size": info.st_size / 1e6,
            "Video": status,
            "needs_video": any(fmt not in inside for fmt in formats) if into_zip else "Not made" in statuses.values(),
        })
    return rows, without_model


def duration(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def start_dir(source):
    """The folder a file dialog should open in."""
    first = source[0]
    return first if first.is_dir() else first.parent


def choose(kind):
    """Button callback: open a native dialog and replace the source with whatever was picked."""
    start = start_dir(st.session_state.source)
    try:
        if kind == "files":
            picked = file_dialog.choose_files(start)
        else:
            folder = file_dialog.choose_folder(start)
            picked = [folder] if folder else []
    except file_dialog.DialogUnavailable as error:
        st.session_state.dialog_error = str(error)
        return
    if picked:  # an empty pick means the dialog was cancelled, so keep the current source
        st.session_state.source = picked


def use_typed_path():
    typed = st.session_state.typed_path.strip()
    if typed:
        st.session_state.source = [Path(typed).expanduser()]


def adjust_orientation(keys, axis=None, change=0):
    if axis is None:
        for key in keys:
            st.session_state[key] = 0
    else:
        value = st.session_state[keys[axis]] + change
        st.session_state[keys[axis]] = (value + 180) % 360 - 180


def orientation_controls(rows, running):
    """Remember adjustments separately from widget state when the chosen model changes."""
    with st.expander("Adjust model orientation", expanded=True):
        st.caption("If a model lies on its side, turn it upright here before it spins. Adjustments apply only to the chosen model and last for this session.")
        paths = [row["path"] for row in rows]
        chosen = st.selectbox("Model to adjust", paths, format_func=lambda path: path.name,
                              disabled=running)
        saved = session()["orientations"].get(chosen, (0, 0, 0))
        keys = [f"orientation-{chosen}-{axis}" for axis in range(3)]
        for key, value in zip(keys, saved):
            if key not in st.session_state:
                st.session_state[key] = value
        columns = st.columns(3)
        values = []
        for axis, (column, label, help_text) in enumerate(zip(columns,
                ("Tilt forward / backward", "Starting direction", "Lean left / right"),
                ("Rotate around the model's X axis. Try 90° for a model lying flat.",
                 "Turn around the upright axis to choose the first view of the video.",
                 "Rotate around the Z axis. Try 90° for a model lying sideways."))):
            values.append(column.slider(label, -180, 180, step=1, format="%d°", key=keys[axis],
                                        disabled=running, help=help_text))
            buttons = column.container(horizontal=True)
            for amount in (-90, 90):
                buttons.button(f"{amount:+}°", key=f"turn-{axis}-{amount}", disabled=running,
                               on_click=adjust_orientation, args=(keys, axis, amount))
        session()["orientations"][chosen] = tuple(values)
        actions = st.container(horizontal=True)
        actions.button("Reset orientation", disabled=running, on_click=adjust_orientation, args=(keys,))
        if actions.button("Preview this model", icon=":material/visibility:", disabled=running):
            start(Job("preview", [chosen], orientations=dict(session()["orientations"]),
                      workspace=session()["workspace"] if uploading else None))
        st.caption("Click Preview this model after adjusting. The looping preview makes a full turn faster than the finished video.")


# Page

st.set_page_config(page_title="Turntable videos", page_icon=":material/3d_rotation:", layout="wide")

job = session()["job"]
running = job is not None and not job.finished

with st.sidebar:
    st.header("How it works")
    st.markdown(
        f"""
Each ZIP or GLB file gets a {render.FRAMES // render.FPS}-second {render.WIDTH}×{render.HEIGHT} video of its model making one full turn. Choose WMV, MP4, or both.

The video takes the file's name, so `Tile 11.zip` gets `Tile 11.wmv`. Acquia DAM looks for that name when it builds a preview for a ZIP.

If a ZIP holds a `.glb` from the GLB texture processor, such as a brightened copy, the app renders that and marks it *processed* in the list, with how many stops the processor brightened it. Otherwise, if a ZIP holds several `.obj` files, the app renders the largest one that has a material file. That's usually the high-detail copy. A ZIP with no textured `.obj` uses its largest `.glb` file instead.

A GLB file uploaded on its own has no ZIP, so its video stays a separate download.

For uploads, videos are added to a temporary copy that you can download. In local file mode, putting videos inside ZIPs changes the ZIP files themselves. The app writes a copy and swaps it in at the end, so a cancelled or crashed run can't damage a ZIP.

MP4 works in browsers and QuickTime. To watch WMV, use VLC or IINA. Use **Preview** to check a model before rendering.
"""
    )

st.title(":material/3d_rotation: Turntable videos")
st.caption("Make WMV or MP4 turntable videos of the 3D models in Pedestal 3D ZIP downloads and GLB files.")

input_mode = "Upload files"
if LOCAL_FILES:
    input_mode = st.radio("Model source", ["Upload files", "Local files"], horizontal=True, disabled=running)
uploading = input_mode == "Upload files"
out_dir = None
if uploading:
    uploaded = st.file_uploader("Upload ZIP or GLB files", type=["zip", "glb"], accept_multiple_files=True,
                                disabled=running)
    source = list(dict.fromkeys(session()["workspace"].save(item) for item in uploaded))
    st.caption("Download the finished videos and updated ZIPs below. Keep this tab open while rendering and download your files before leaving.")
    zip_col = st.container()
else:
    if "source" not in st.session_state:
        downloads = Path.home() / "Downloads"
        st.session_state.source = [downloads if downloads.is_dir() else Path.home()]

    st.markdown("**ZIP and GLB files**")
    source_bar = st.container(horizontal=True, vertical_alignment="center")
    source_bar.button("Choose files…", icon=":material/folder_zip:", on_click=choose, args=("files",), disabled=running)
    source_bar.button("Choose a folder…", icon=":material/folder_open:", on_click=choose, args=("folder",), disabled=running)
    with source_bar.popover("Type a path", icon=":material/keyboard:", disabled=running):
        st.text_input(
            "Folder, ZIP or GLB file",
            key="typed_path",
            placeholder=str(Path.home() / "Downloads"),
            on_change=use_typed_path,
            help="Press Enter to use it.",
        )
    source_bar.button("Refresh", icon=":material/refresh:", type="tertiary", disabled=running)

    if error := st.session_state.pop("dialog_error", None):
        st.error(f"Couldn't open a file dialog ({error}). Use **Upload files** instead.", icon=":material/error:")

    source = st.session_state.source
    missing = [path for path in source if not path.exists()]
    source = [path for path in source if path.exists()]
    if len(source) == 1 and source[0].is_dir():
        st.markdown(f":material/folder: Every ZIP and GLB file in `{source[0]}`")
    elif len(source) == 1:
        st.markdown(f":material/folder_zip: `{source[0]}`")
    elif source:
        st.markdown(f":material/folder_zip: {len(source)} files you chose")
    for path in missing:
        st.warning(f"Can't find {path}", icon=":material/folder_off:")

    where_col, out_col, zip_col = st.columns([2, 3, 2], vertical_alignment="bottom")
    where = where_col.radio("Save videos", ["Next to each file", "In another folder"], horizontal=True, disabled=running)
    out_dir = None
    if where == "In another folder":
        default_out = start_dir(source) / "Turntable videos" if source else Path.home() / "Turntable videos"
        out_dir = Path(out_col.text_input("Output folder", value=str(default_out), disabled=running).strip()).expanduser()
add_to_zip = zip_col.toggle(
    "Put each video inside its ZIP",
    disabled=running,
    help="Acquia DAM builds a ZIP's preview from the WMV inside it that has the same name as the ZIP. "
         "GLB files on their own have no ZIP, so their videos stay separate.",
)
format_choice = st.selectbox("Video format", ["WMV", "MP4", "WMV + MP4"], disabled=running,
                             help="Use WMV for Acquia DAM previews. MP4 plays in browsers and QuickTime.")
formats = render.video_formats("both" if format_choice == "WMV + MP4" else format_choice.lower())

st.divider()

rows, without_model = scan(source, out_dir, add_to_zip, formats) if source else ([], 0)
selected = []

if not source:
    st.info("Upload some ZIP or GLB files to get started." if uploading else "Choose some ZIP or GLB files, or a folder, to get started.", icon=":material/folder_zip:")
elif not rows:
    st.info("None of these ZIP files has an .obj or .glb model inside.", icon=":material/inventory_2:")
else:
    # Reset the ticks whenever the list or a status changes, so finished ZIPs drop out of the selection
    signature = tuple((str(row["path"]), row["Video"], row["needs_video"]) for row in rows)
    if st.session_state.get("signature") != signature:
        st.session_state.signature = signature
        st.session_state.ticks = {str(row["path"]): row["needs_video"] for row in rows}
        st.session_state.table_version = st.session_state.get("table_version", 0) + 1

    count_col, need_col, selected_col, pick_col = st.columns([1, 1, 1, 3], vertical_alignment="bottom")
    count_col.metric("Models", len(rows))
    need_col.metric("Need a video", sum(row["needs_video"] for row in rows))

    picks = pick_col.container(horizontal=True, horizontal_alignment="right")
    for label, rule in (
        ("Select all", lambda row: True),
        ("Select none", lambda row: False),
        ("Select ones that need a video", lambda row: row["needs_video"]),
    ):
        if picks.button(label, type="tertiary", disabled=running):
            st.session_state.ticks = {str(row["path"]): rule(row) for row in rows}
            st.session_state.table_version += 1

    # Only show where each ZIP lives when they come from more than one folder
    columns = ["File", "Folder", "Model", "Size", "Video"] if not uploading and len({row["Folder"] for row in rows}) > 1 else ["File", "Model", "Size", "Video"]
    table = pd.DataFrame(
        [{"Include": st.session_state.ticks.get(str(row["path"]), False), **{k: row[k] for k in columns}} for row in rows]
    )
    edited = st.data_editor(
        table,
        key=f"zips-{st.session_state.table_version}",
        hide_index=True,
        disabled=True if running else columns,
        column_config={
            "Include": st.column_config.CheckboxColumn("", width="small"),
            "File": st.column_config.TextColumn("File", width="large"),
            "Folder": st.column_config.TextColumn("Folder", width="medium"),
            "Model": st.column_config.TextColumn("Model to render", width="medium"),
            "Size": st.column_config.NumberColumn("Size", format="%.1f MB", width="small"),
            "Video": st.column_config.TextColumn("Video", width="medium"),
        },
    )
    selected = [row["path"] for row, include in zip(rows, edited["Include"]) if include]
    selected_col.metric("Selected", len(selected))
    if without_model:
        st.caption(f"{without_model} of these ZIP files had no .obj or .glb model and aren't listed.")

    orientation_controls(rows, running)

    actions = st.container(horizontal=True)
    plural = "s" if len(selected) != 1 else ""
    if actions.button(f"Preview {len(selected)} model{plural}", icon=":material/visibility:", disabled=running or not selected):
        start(Job("preview", selected, orientations=dict(session()["orientations"]),
                  workspace=session()["workspace"] if uploading else None))
    if actions.button(
        f"Make {len(selected)} video{plural}",
        type="primary",
        icon=":material/movie:",
        disabled=running or not selected or (out_dir is not None and not str(out_dir).strip()),
    ):
        start(Job("render", selected, out_dir=out_dir, add_to_zip=add_to_zip, formats=formats,
                  orientations=dict(session()["orientations"]),
                  workspace=session()["workspace"] if uploading else None))


@st.fragment(run_every=0.5)
def progress_panel(job):
    if job.finished:
        st.rerun()  # redraw the whole page, which re-enables the controls and updates the statuses
    total = len(job.sources)
    in_progress = job.frame / job.total_frames
    fraction = min((job.done + in_progress) / total, 1.0)
    elapsed = time.time() - job.started

    with st.container(border=True):
        verb = "Making video" if job.kind == "render" else "Previewing"
        st.progress(fraction, text=f"**{verb} {min(job.done + 1, total)} of {total}:** {job.current}")
        details = [job.stage or "Starting"]
        if job.stage.startswith("Rendering"):
            details.append(f"frame {job.frame} of {job.total_frames}")
        details.append(f"{duration(elapsed)} elapsed")
        if fraction > 0.02:
            details.append(f"about {duration(elapsed / fraction - elapsed)} left")
        if job.cancel.is_set():
            details.append("cancelling after this step")
        st.caption(" · ".join(details))
        if st.button("Cancel", icon=":material/stop_circle:", disabled=job.cancel.is_set()):
            job.cancel.set()
            st.rerun(scope="fragment")


def show_results(job):
    counts = Counter(outcome for _, outcome, _ in job.results)
    header, dismiss = st.columns([5, 1], vertical_alignment="center")
    header.subheader("Previews" if job.kind == "preview" else "Results")
    if dismiss.button("Clear", icon=":material/close:", type="tertiary", width="stretch"):
        session()["job"] = None
        st.rerun()

    if job.error:
        st.error(job.error, icon=":material/error:")
    summary = []
    if job.kind == "render":
        summary.append(f"{counts['Done']} model{'s' if counts['Done'] != 1 else ''} rendered")
    for outcome in ("Skipped", "Cancelled", "Failed"):
        if counts[outcome]:
            summary.append(f"{counts[outcome]} {outcome.lower()}")
    if summary:
        message = ", ".join(summary).capitalize() + f" in {duration(job.ended - job.started)}"
        if counts["Failed"]:
            st.error(message, icon=":material/error:")
        elif counts["Cancelled"] or counts["Skipped"]:
            st.warning(message, icon=":material/warning:")
        else:
            st.success(message, icon=":material/check_circle:")

    problems = [result for result in job.results if result[1] in ("Skipped", "Cancelled", "Failed")]
    if job.kind == "render":
        st.dataframe(pd.DataFrame(job.results, columns=["File", "Result", "Detail"]), hide_index=True)
    elif problems:
        st.dataframe(pd.DataFrame(problems, columns=["File", "Result", "Detail"]), hide_index=True)

    if job.workspace and job.downloads:
        for i, path in enumerate(job.downloads):
            mime = {".zip": "application/zip", ".wmv": "video/x-ms-wmv", ".mp4": "video/mp4"}[path.suffix.lower()]
            with path.open("rb") as data:
                st.download_button(f"Download {path.name}", data, file_name=path.name,
                                   mime=mime, key=f"download-{i}", on_click="ignore")

    if job.previews:
        st.caption("Check the full turn and the four views before making the video. Both use the orientation shown below.")
        columns = st.columns(min(2, len(job.previews)))
        for i, (path, model, triangles, image, animation) in enumerate(job.previews):
            with columns[i % len(columns)].container(border=True):
                st.markdown(f"**{path.name}**")
                st.caption(f"{model} · {triangles:,} triangles")
                angles = job.orientations.get(path, (0, 0, 0))
                st.caption(f"Tilt {angles[0]}° · Starting direction {angles[1]}° · Lean {angles[2]}°")
                if angles != session()["orientations"].get(path, (0, 0, 0)):
                    st.warning("Orientation changed. Click Preview this model to refresh this preview before rendering.")
                st.image(animation, width="stretch")
                st.image(image, width="stretch")


if running:
    progress_panel(job)
elif job is not None:
    show_results(job)
