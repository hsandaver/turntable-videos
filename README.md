# Turntable videos

Makes WMV and MP4 turntable videos of the 3D models in [Pedestal 3D](https://unimelb.pedestal3d.com/) ZIP downloads and in GLB files. Use WMV for ZIP previews in Acquia DAM, MP4 for playback in browsers and QuickTime, or create both.

Each ZIP or GLB file gets a 15-second, 1920×1080 video of its model making one full turn. The video takes the file's name, so `Tile 11.zip` gets `Tile 11.wmv` and `Chair.glb` gets `Chair.wmv`. Acquia DAM looks for a WMV with the ZIP's name when it builds a ZIP's preview, and the app can put the video inside the ZIP for you.

It runs on Windows, macOS and Linux, or on Streamlit Community Cloud. There's a Streamlit app and a command-line script, and both live in `outputs/tile11-sample/`.

## Setup

You need Python 3.12 or 3.13, and a graphics driver with OpenGL 3.3 or newer. On Python 3.14 the OpenGL packages have to be compiled from source, which fails on most Windows and Linux machines.

On macOS or Linux:

```bash
cd outputs/tile11-sample
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, in PowerShell:

```powershell
cd outputs\tile11-sample
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

In each new terminal window, `cd` into `outputs/tile11-sample` and activate `.venv` again before running anything.

## The app

Run this from `outputs/tile11-sample`. The app's colour theme only loads from that folder.

```bash
streamlit run app.py
```

It opens in your browser.

1. Upload your ZIP or GLB files. The app lists each GLB file, and each ZIP with an `.obj` or `.glb` model inside, and shows whether it already has a video.
2. Choose **WMV**, **MP4**, or **WMV + MP4** under **Video format**. Switch on **Put each video inside its ZIP** to include the selected formats in the ZIP. Choose WMV or both for Acquia DAM. A GLB file uploaded on its own has no ZIP, so its video is always a separate download.
3. Tick the files you want. Files that still need a video are ticked already.
4. Open **Adjust model orientation** and choose a model. If it lies on its side, use the **Tilt forward / backward** or **Lean left / right** controls, including the **−90°** and **+90°** buttons, to stand it upright. Use **Starting direction** to choose its first view. Click **Preview this model** after changes to see a looping full turn and four still views. The loop runs faster than the final video. Each model keeps its own adjustments for the current browser session; **Reset orientation** restores the original orientation. The selected models' previews and videos use these same settings.
5. Click **Make videos**. A progress bar shows how far along it is, and you can cancel.
6. Download the finished videos and, if requested, the ZIPs containing them. Keep the tab open while rendering. Files are temporary and belong to your browser session, so download them before refreshing or leaving the page.

Browsers can't play WMV, and neither can QuickTime. To watch a finished video, use [VLC](https://www.videolan.org/) or, on a Mac, [IINA](https://iina.io/).

To work directly with files on your own computer, set `TURNTABLE_LOCAL_FILES=1` before starting Streamlit, then choose **Local files**. This enables the native file and folder pickers, **Type a path**, and output folder settings. On macOS or Linux, run `TURNTABLE_LOCAL_FILES=1 streamlit run app.py`. In PowerShell, set `$env:TURNTABLE_LOCAL_FILES = "1"` and then run `streamlit run app.py`. On Linux, the file dialogs need `zenity`, `kdialog` or Python's tkinter. Leave this option disabled on hosted servers.

## The command line

`render.py` does the same work without the app. It takes ZIP and GLB files, or folders of them:

```bash
python render.py "Tile 11.zip"                # writes "Tile 11.wmv" next to the ZIP
python render.py Chair.glb                    # writes "Chair.wmv" next to the GLB
python render.py path/to/folder               # every ZIP and GLB file in the folder
python render.py --format mp4 path/to/folder   # MP4 files instead of WMV
python render.py --format both path/to/folder  # WMV and MP4 files
python render.py --preview path/to/folder     # one still (.png) per file instead of a video
python render.py --add-to-zip path/to/folder  # also put each video inside its ZIP
python render.py --out videos path/to/folder  # save the output somewhere else
```

WMV is the default. MP4 files also take the source file's name, so `Tile 11.zip` gets `Tile 11.mp4`. With `--add-to-zip`, formats already inside the ZIP are skipped individually, so you can add MP4 to a ZIP that already contains WMV. `--add-to-zip` leaves GLB files alone and just saves their videos.

## How it works

The script reads the model straight out of the ZIP without unpacking it. If a ZIP holds several OBJ files, as Pedestal 3D downloads often do with low, medium and high detail copies, it renders the largest one that has a material file. A ZIP with no textured OBJ file uses its largest GLB file instead.

GLB files use each material's base colour texture, or its flat base colour when there's no texture. Other material maps, such as normal and roughness maps, don't show, because the renderer draws textures as they are without lighting. The script can't read GLB files that need Draco or meshopt compression or KTX2 textures, and it says so instead of rendering them. Export those again without compression to render them.

Rendering uses OpenGL through [moderngl](https://github.com/moderngl/moderngl), with no lighting, because the scan textures already have lighting baked in. The app applies each model's orientation adjustments before spinning it around the vertical axis. Framing fits the whole turn so tilted corners stay visible. The command-line script uses the model's original orientation. The frames go straight into ffmpeg, which encodes them as WMV8 or H.264 MP4. MP4 uses the yuv420p pixel format and puts playback metadata at the start of the file. Choosing both formats renders the model once for each format. [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) ships its own copy of ffmpeg, so you don't need to install it.

Videos are written under a temporary name and renamed when finished, so a cancelled or crashed render never leaves a broken video behind. Adding a video to a ZIP works the same way: the app writes a new copy of the ZIP and swaps it in at the end.

`render.swift` is an earlier macOS-only version using SceneKit. It compiles, but it renders models without their textures.

## Streamlit Community Cloud

Deploy `outputs/tile11-sample/app.py` with Python 3.12 or 3.13. The adjacent `requirements.txt` installs the Python dependencies. The `packages.txt` at the repository root installs the Linux graphics libraries for headless rendering with EGL. Community Cloud requires this file at the root, even when the app is in a subdirectory. See [Streamlit's dependency documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

The default interface uses browser uploads and downloads, so no tkinter or desktop file dialog is needed. Each browser session has its own job and temporary workspace. Uploaded originals on your computer stay unchanged.

Cloud rendering uses software OpenGL and can be slow or run out of memory with large models. Start with one ZIP and use **Preview** before making a video. Streamlit's default upload limit is 200 MB per file. To change it on Community Cloud, set `[server]` and `maxUploadSize` in a `.streamlit/config.toml` at the repository root. Larger uploads also need more memory during model loading and downloads.

## Models

This repository contains no model data. Models downloaded from Pedestal 3D come with their own licences. Check each one before sharing its video.
