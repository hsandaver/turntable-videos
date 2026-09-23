"""Streamlit app for making turntable videos from Pedestal 3D ZIP downloads.

    pip install -r requirements.txt
    streamlit run app.py

The rendering itself lives in render.py, which also works on its own from the command line.
"""
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

PREVIEW_SIZE = 360  # pixels per view in the preview strips
LOCAL_FILES = os.environ.get("TURNTABLE_LOCAL_FILES") == "1"


class Cancelled(Exception):
    pass


@dataclass
class Job:
    """A preview or render run. A background thread fills it in while the page polls it."""

    kind: str  # "preview" or "render"
    zips: list
    out_dir: Path | None = None
    add_to_zip: bool = False
    formats: tuple = ("wmv",)
    workspace: UploadWorkspace | None = None  # keep temporary files alive during background work
    downloads: list = field(default_factory=list)
    done: int = 0
    current: str = ""
    stage: str = ""
    frame: int = 0
    total_frames: int = render.FRAMES
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    results: list = field(default_factory=list)  # (ZIP name, outcome, detail)
    previews: list = field(default_factory=list)  # (ZIP name, model, triangles, JPEG bytes)
    error: str = ""
    finished: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)


def session():
    """Keep jobs and uploads private to this browser session."""
    if "work" not in st.session_state:
        st.session_state.work = {"job": None, "workspace": UploadWorkspace()}
    return st.session_state.work


# Background work. Nothing here calls st.*, because Streamlit commands only work on the page's own thread.

def run_job(job):
    try:
        # The OpenGL context has to be created on the thread that uses it
        renderer = render.Renderer()
    except Exception as error:
        job.error = f"Could not start OpenGL: {error}"
        job.ended, job.finished = time.time(), True
        return
    try:
        for zip_path in job.zips:
            if job.cancel.is_set():
                break
            job.current, job.stage, job.frame = zip_path.name, "Loading model", 0
            try:
                (preview_zip if job.kind == "preview" else render_zip)(renderer, zip_path, job)
            except Cancelled:
                job.results.append((zip_path.name, "Cancelled", "Stopped before the video was finished"))
            except Exception as error:
                job.results.append((zip_path.name, "Failed", str(error)))
            finally:
                renderer.unload()
                job.done += 1
    finally:
        renderer.release()
        job.ended, job.finished = time.time(), True


def preview_zip(renderer, zip_path, job):
    with zipfile.ZipFile(zip_path) as zf:
        obj = render.pick_obj(zf)
        if obj is None:
            job.results.append((zip_path.name, "Skipped", "No .obj model in this ZIP"))
            return
        triangles = renderer.load(zf, obj.filename)

    job.stage = "Rendering views"
    # A model turning in place never leaves the centre square of the frame, so crop to that
    left = (render.WIDTH - render.HEIGHT) // 2
    strip = Image.new("RGB", (PREVIEW_SIZE * 4, PREVIEW_SIZE))
    for k in range(4):
        view = Image.fromarray(renderer.frame(k * render.FRAMES // 4))
        view = view.crop((left, 0, left + render.HEIGHT, render.HEIGHT))
        strip.paste(view.resize((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS), (k * PREVIEW_SIZE, 0))
    jpeg = BytesIO()
    strip.save(jpeg, "JPEG", quality=88)
    job.previews.append((zip_path.name, obj.filename, triangles, jpeg.getvalue()))
    job.results.append((zip_path.name, "Previewed", obj.filename))


def render_zip(renderer, zip_path, job):
    out_dir = job.out_dir or zip_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    videos = [out_dir / f"{zip_path.stem}.{fmt}" for fmt in job.formats]

    with zipfile.ZipFile(zip_path) as zf:
        if job.add_to_zip:
            videos = [video for video in videos if video.name not in zf.namelist()]
            if not videos:
                job.results.append((zip_path.name, "Skipped", "The ZIP already contains the selected formats"))
                return
        obj = render.pick_obj(zf)
        if obj is None:
            job.results.append((zip_path.name, "Skipped", "No .obj model in this ZIP"))
            return
        renderer.load(zf, obj.filename)

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
            f"Saved {video.name} " + ("next to the ZIP" if job.out_dir is None else f"in {job.out_dir}")
        )
        job.downloads.append(video)
        if job.add_to_zip:
            job.stage = "Adding the video to the ZIP"
            render.add_to_zip(zip_path, video)
            if zip_path not in job.downloads:
                job.downloads.append(zip_path)
            saved += " and added it to the ZIP"
        details.append(saved)
    job.results.append((zip_path.name, "Done", "; ".join(details)))


def start(job):
    session()["job"] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    st.rerun()


# Folder scanning

@st.cache_data(show_spinner=False)
def inspect_zip(path, modified, size, formats):
    """Return the model filename and formats inside the ZIP. `modified` and `size` bust the cache."""
    try:
        with zipfile.ZipFile(path) as zf:
            obj = render.pick_obj(zf)
            inside = {fmt for fmt in formats if f"{Path(path).stem}.{fmt}" in zf.namelist()}
            return (obj.filename if obj else None), inside
    except (zipfile.BadZipFile, OSError):
        return None, set()


def scan(source, out_dir, add_to_zip, formats):
    rows, without_model = [], 0
    for zip_path in render.find_zips(source):
        info = zip_path.stat()
        model, inside = inspect_zip(str(zip_path), info.st_mtime, info.st_size, formats)
        if model is None:
            without_model += 1
            continue
        statuses = {}
        for fmt in formats:
            saved = ((out_dir or zip_path.parent) / f"{zip_path.stem}.{fmt}").exists()
            statuses[fmt] = "In ZIP" if fmt in inside else "Saved" if saved else "Not made"
        status = ", ".join(f"{fmt.upper()}: {value}" for fmt, value in statuses.items())
        rows.append({
            "path": zip_path,
            "ZIP": zip_path.name,
            "Folder": str(zip_path.parent),
            "Model": model,
            "Size": info.st_size / 1e6,
            "Video": status,
            "needs_video": any(fmt not in inside for fmt in formats) if add_to_zip else "Not made" in statuses.values(),
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
            picked = file_dialog.choose_zip_files(start)
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


# Page

st.set_page_config(page_title="Turntable videos", page_icon=":material/3d_rotation:", layout="wide")

job = session()["job"]
running = job is not None and not job.finished

with st.sidebar:
    st.header("How it works")
    st.markdown(
        f"""
Each ZIP gets a {render.FRAMES // render.FPS}-second {render.WIDTH}×{render.HEIGHT} video of its model making one full turn. Choose WMV, MP4, or both.

The video takes the ZIP's name, so `Tile 11.zip` gets `Tile 11.wmv`. Acquia DAM looks for that name when it builds a preview for a ZIP.

If a ZIP holds several `.obj` files, the app renders the largest one that has a material file. That's usually the high-detail copy.

For uploads, videos are added to a temporary copy that you can download. In local file mode, putting videos inside ZIPs changes the ZIP files themselves. The app writes a copy and swaps it in at the end, so a cancelled or crashed run can't damage a ZIP.

MP4 works in browsers and QuickTime. To watch WMV, use VLC or IINA. Use **Preview** to check a model before rendering.
"""
    )

st.title(":material/3d_rotation: Turntable videos")
st.caption("Make WMV or MP4 turntable videos of the 3D models in Pedestal 3D ZIP downloads.")

input_mode = "Upload ZIP files"
if LOCAL_FILES:
    input_mode = st.radio("ZIP source", ["Upload ZIP files", "Local files"], horizontal=True, disabled=running)
uploading = input_mode == "Upload ZIP files"
out_dir = None
if uploading:
    uploaded = st.file_uploader("Upload ZIP files", type=["zip"], accept_multiple_files=True, disabled=running)
    source = list(dict.fromkeys(session()["workspace"].save(item) for item in uploaded))
    st.caption("Download the finished videos and updated ZIPs below. Keep this tab open while rendering and download your files before leaving.")
    zip_col = st.container()
else:
    if "source" not in st.session_state:
        downloads = Path.home() / "Downloads"
        st.session_state.source = [downloads if downloads.is_dir() else Path.home()]

    st.markdown("**ZIP files**")
    source_bar = st.container(horizontal=True, vertical_alignment="center")
    source_bar.button("Choose ZIP files…", icon=":material/folder_zip:", on_click=choose, args=("files",), disabled=running)
    source_bar.button("Choose a folder…", icon=":material/folder_open:", on_click=choose, args=("folder",), disabled=running)
    with source_bar.popover("Type a path", icon=":material/keyboard:", disabled=running):
        st.text_input(
            "Folder or ZIP file",
            key="typed_path",
            placeholder=str(Path.home() / "Downloads"),
            on_change=use_typed_path,
            help="Press Enter to use it.",
        )
    source_bar.button("Refresh", icon=":material/refresh:", type="tertiary", disabled=running)

    if error := st.session_state.pop("dialog_error", None):
        st.error(f"Couldn't open a file dialog ({error}). Use **Upload ZIP files** instead.", icon=":material/error:")

    source = st.session_state.source
    missing = [path for path in source if not path.exists()]
    source = [path for path in source if path.exists()]
    if len(source) == 1 and source[0].is_dir():
        st.markdown(f":material/folder: Every ZIP in `{source[0]}`")
    elif len(source) == 1:
        st.markdown(f":material/folder_zip: `{source[0]}`")
    elif source:
        st.markdown(f":material/folder_zip: {len(source)} ZIP files you chose")
    for path in missing:
        st.warning(f"Can't find {path}", icon=":material/folder_off:")

    where_col, out_col, zip_col = st.columns([2, 3, 2], vertical_alignment="bottom")
    where = where_col.radio("Save videos", ["Next to each ZIP", "In another folder"], horizontal=True, disabled=running)
    out_dir = None
    if where == "In another folder":
        default_out = start_dir(source) / "Turntable videos" if source else Path.home() / "Turntable videos"
        out_dir = Path(out_col.text_input("Output folder", value=str(default_out), disabled=running).strip()).expanduser()
add_to_zip = zip_col.toggle(
    "Put each video inside its ZIP",
    disabled=running,
    help="Acquia DAM builds a ZIP's preview from the WMV inside it that has the same name as the ZIP.",
)
format_choice = st.selectbox("Video format", ["WMV", "MP4", "WMV + MP4"], disabled=running,
                             help="Use WMV for Acquia DAM previews. MP4 plays in browsers and QuickTime.")
formats = render.video_formats("both" if format_choice == "WMV + MP4" else format_choice.lower())

st.divider()

rows, without_model = scan(source, out_dir, add_to_zip, formats) if source else ([], 0)
selected = []

if not source:
    st.info("Upload some ZIP files to get started." if uploading else "Choose some ZIP files or a folder to get started.", icon=":material/folder_zip:")
elif not rows:
    st.info("None of these ZIP files has an .obj model inside.", icon=":material/inventory_2:")
else:
    # Reset the ticks whenever the list or a status changes, so finished ZIPs drop out of the selection
    signature = tuple((str(row["path"]), row["Video"], row["needs_video"]) for row in rows)
    if st.session_state.get("signature") != signature:
        st.session_state.signature = signature
        st.session_state.ticks = {str(row["path"]): row["needs_video"] for row in rows}
        st.session_state.table_version = st.session_state.get("table_version", 0) + 1

    count_col, need_col, selected_col, pick_col = st.columns([1, 1, 1, 3], vertical_alignment="bottom")
    count_col.metric("Model ZIPs", len(rows))
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
    columns = ["ZIP", "Folder", "Model", "Size", "Video"] if not uploading and len({row["Folder"] for row in rows}) > 1 else ["ZIP", "Model", "Size", "Video"]
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
            "ZIP": st.column_config.TextColumn("ZIP file", width="large"),
            "Folder": st.column_config.TextColumn("Folder", width="medium"),
            "Model": st.column_config.TextColumn("Model to render", width="medium"),
            "Size": st.column_config.NumberColumn("Size", format="%.1f MB", width="small"),
            "Video": st.column_config.TextColumn("Video", width="medium"),
        },
    )
    selected = [row["path"] for row, include in zip(rows, edited["Include"]) if include]
    selected_col.metric("Selected", len(selected))
    if without_model:
        st.caption(f"{without_model} of these ZIP files had no .obj model and aren't listed.")

    actions = st.container(horizontal=True)
    plural = "s" if len(selected) != 1 else ""
    if actions.button(f"Preview {len(selected)} model{plural}", icon=":material/visibility:", disabled=running or not selected):
        start(Job("preview", selected, workspace=session()["workspace"] if uploading else None))
    if actions.button(
        f"Make {len(selected)} video{plural}",
        type="primary",
        icon=":material/movie:",
        disabled=running or not selected or (out_dir is not None and not str(out_dir).strip()),
    ):
        start(Job("render", selected, out_dir=out_dir, add_to_zip=add_to_zip, formats=formats,
                  workspace=session()["workspace"] if uploading else None))


@st.fragment(run_every=0.5)
def progress_panel(job):
    if job.finished:
        st.rerun()  # redraw the whole page, which re-enables the controls and updates the statuses
    total = len(job.zips)
    in_progress = job.frame / job.total_frames if job.kind == "render" else 0
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
        summary.append(f"{counts['Done']} ZIP{'s' if counts['Done'] != 1 else ''} rendered")
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
        st.dataframe(pd.DataFrame(job.results, columns=["ZIP file", "Result", "Detail"]), hide_index=True)
    elif problems:
        st.dataframe(pd.DataFrame(problems, columns=["ZIP file", "Result", "Detail"]), hide_index=True)

    if job.workspace and job.downloads:
        for i, path in enumerate(job.downloads):
            mime = {".zip": "application/zip", ".wmv": "video/x-ms-wmv", ".mp4": "video/mp4"}[path.suffix.lower()]
            with path.open("rb") as data:
                st.download_button(f"Download {path.name}", data, file_name=path.name,
                                   mime=mime, key=f"download-{i}", on_click="ignore")

    if job.previews:
        st.caption("Each model from the front, then turned a quarter at a time. Check that it's upright and textured before making its video.")
        columns = st.columns(2)
        for i, (name, model, triangles, image) in enumerate(job.previews):
            with columns[i % 2].container(border=True):
                st.markdown(f"**{name}**")
                st.caption(f"{model} · {triangles:,} triangles")
                st.image(image, width="stretch")


if running:
    progress_panel(job)
elif job is not None:
    show_results(job)
