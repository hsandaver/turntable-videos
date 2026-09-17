"""Streamlit app for making turntable videos from Pedestal 3D ZIP downloads.

    pip install -r requirements.txt
    streamlit run app.py

The rendering itself lives in render.py, which also works on its own from the command line.
"""
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

PREVIEW_SIZE = 360  # pixels per view in the preview strips


class Cancelled(Exception):
    pass


@dataclass
class Job:
    """A preview or render run. A background thread fills it in while the page polls it."""

    kind: str  # "preview" or "render"
    zips: list
    out_dir: Path | None = None
    add_to_zip: bool = False
    done: int = 0
    current: str = ""
    stage: str = ""
    frame: int = 0
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    results: list = field(default_factory=list)  # (ZIP name, outcome, detail)
    previews: list = field(default_factory=list)  # (ZIP name, model, triangles, JPEG bytes)
    error: str = ""
    finished: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)


@st.cache_resource
def shared():
    """Holds the running or most recent job. It's shared across sessions, so a browser refresh finds a running job again."""
    return {"job": None}


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
    video = out_dir / f"{zip_path.stem}.wmv"

    with zipfile.ZipFile(zip_path) as zf:
        if job.add_to_zip and video.name in zf.namelist():
            job.results.append((zip_path.name, "Skipped", "The ZIP already contains its video"))
            return
        obj = render.pick_obj(zf)
        if obj is None:
            job.results.append((zip_path.name, "Skipped", "No .obj model in this ZIP"))
            return
        renderer.load(zf, obj.filename)

    def on_frame(frames_done):
        job.frame = frames_done
        if job.cancel.is_set():
            raise Cancelled

    job.stage = "Rendering"
    render.encode(renderer, video, on_frame)

    saved = f"Saved {video.name} " + ("next to the ZIP" if job.out_dir is None else f"in {job.out_dir}")
    if job.add_to_zip:
        job.stage = "Adding the video to the ZIP"
        render.add_to_zip(zip_path, video)
        saved += " and added it to the ZIP"
    job.results.append((zip_path.name, "Done", saved))


def start(job):
    shared()["job"] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    st.rerun()


# Folder scanning

@st.cache_data(show_spinner=False)
def inspect_zip(path, modified, size):
    """Return (model filename or None, whether the ZIP already holds its video). `modified` and `size` bust the cache."""
    try:
        with zipfile.ZipFile(path) as zf:
            obj = render.pick_obj(zf)
            return (obj.filename if obj else None), f"{Path(path).stem}.wmv" in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None, False


def scan(source, out_dir, add_to_zip):
    rows, without_model = [], 0
    for zip_path in render.find_zips(source):
        info = zip_path.stat()
        model, inside = inspect_zip(str(zip_path), info.st_mtime, info.st_size)
        if model is None:
            without_model += 1
            continue
        saved = ((out_dir or zip_path.parent) / f"{zip_path.stem}.wmv").exists()
        status = "In ZIP" if inside else "Saved" if saved else "Not made"
        rows.append({
            "path": zip_path,
            "ZIP": zip_path.name,
            "Folder": str(zip_path.parent),
            "Model": model,
            "Size": info.st_size / 1e6,
            "Video": status,
            "needs_video": not inside if add_to_zip else status == "Not made",
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

job = shared()["job"]
running = job is not None and not job.finished

with st.sidebar:
    st.header("How it works")
    st.markdown(
        f"""
Each ZIP gets a {render.FRAMES // render.FPS}-second {render.WIDTH}×{render.HEIGHT} WMV of its model making one full turn.

The video takes the ZIP's name, so `Tile 11.zip` gets `Tile 11.wmv`. Acquia DAM looks for that name when it builds a preview for a ZIP.

If a ZIP holds several `.obj` files, the app renders the largest one that has a material file. That's usually the high-detail copy.

Putting videos inside ZIPs changes the ZIP files themselves. The app writes a copy and swaps it in at the end, so a cancelled or crashed run can't damage a ZIP.

Browsers can't play WMV. Use **Preview** to check a model before rendering, and VLC or IINA to watch the finished video.
"""
    )

st.title(":material/3d_rotation: Turntable videos")
st.caption("Make WMV turntable videos of the 3D models in Pedestal 3D ZIP downloads, ready for Acquia DAM.")

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
    st.error(f"Couldn't open a file dialog ({error}). Use **Type a path** instead.", icon=":material/error:")

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

st.divider()

rows, without_model = scan(source, out_dir, add_to_zip) if source else ([], 0)
selected = []

if not source:
    st.info("Choose some ZIP files or a folder to get started.", icon=":material/folder_zip:")
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
    columns = ["ZIP", "Folder", "Model", "Size", "Video"] if len({row["Folder"] for row in rows}) > 1 else ["ZIP", "Model", "Size", "Video"]
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
            "Video": st.column_config.TextColumn("Video", width="small"),
        },
    )
    selected = [row["path"] for row, include in zip(rows, edited["Include"]) if include]
    selected_col.metric("Selected", len(selected))
    if without_model:
        st.caption(f"{without_model} of these ZIP files had no .obj model and aren't listed.")

    actions = st.container(horizontal=True)
    plural = "s" if len(selected) != 1 else ""
    if actions.button(f"Preview {len(selected)} model{plural}", icon=":material/visibility:", disabled=running or not selected):
        start(Job("preview", selected))
    if actions.button(
        f"Make {len(selected)} video{plural}",
        type="primary",
        icon=":material/movie:",
        disabled=running or not selected or (out_dir is not None and not str(out_dir).strip()),
    ):
        start(Job("render", selected, out_dir=out_dir, add_to_zip=add_to_zip))


@st.fragment(run_every=0.5)
def progress_panel(job):
    if job.finished:
        st.rerun()  # redraw the whole page, which re-enables the controls and updates the statuses
    total = len(job.zips)
    in_progress = job.frame / render.FRAMES if job.kind == "render" else 0
    fraction = min((job.done + in_progress) / total, 1.0)
    elapsed = time.time() - job.started

    with st.container(border=True):
        verb = "Making video" if job.kind == "render" else "Previewing"
        st.progress(fraction, text=f"**{verb} {min(job.done + 1, total)} of {total}:** {job.current}")
        details = [job.stage or "Starting"]
        if job.stage == "Rendering":
            details.append(f"frame {job.frame} of {render.FRAMES}")
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
        shared()["job"] = None
        st.rerun()

    if job.error:
        st.error(job.error, icon=":material/error:")
    summary = []
    if job.kind == "render":
        summary.append(f"{counts['Done']} video{'s' if counts['Done'] != 1 else ''} made")
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
