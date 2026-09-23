# Turntable videos

Makes WMV turntable videos of the 3D models in [Pedestal 3D](https://unimelb.pedestal3d.com/) ZIP downloads, for use as ZIP previews in Acquia DAM.

Each ZIP gets a 15-second, 1920×1080 video of its model making one full turn. The video takes the ZIP's name, so `Tile 11.zip` gets `Tile 11.wmv`. Acquia DAM looks for a WMV with that name when it builds a ZIP's preview, and the app can put the video inside the ZIP for you.

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

1. Upload your ZIP files. The app lists each ZIP with an `.obj` model inside and shows whether it already has a video.
2. Switch on **Put each video inside its ZIP** if they're going into Acquia DAM.
3. Tick the ZIPs you want. ZIPs that still need a video are ticked already.
4. Click **Preview** to see each model from four sides before you render. Check that it's upright and textured.
5. Click **Make videos**. A progress bar shows how far along it is, and you can cancel.
6. Download the finished WMVs and, if requested, the ZIPs containing them. Keep the tab open while rendering. Files are temporary and belong to your browser session, so download them before refreshing or leaving the page.

Browsers can't play WMV, and neither can QuickTime. To watch a finished video, use [VLC](https://www.videolan.org/) or, on a Mac, [IINA](https://iina.io/).

To work directly with files on your own computer, set `TURNTABLE_LOCAL_FILES=1` before starting Streamlit, then choose **Local files**. This enables the native file and folder pickers, **Type a path**, and output folder settings. On macOS or Linux, run `TURNTABLE_LOCAL_FILES=1 streamlit run app.py`. In PowerShell, set `$env:TURNTABLE_LOCAL_FILES = "1"` and then run `streamlit run app.py`. On Linux, the file dialogs need `zenity`, `kdialog` or Python's tkinter. Leave this option disabled on hosted servers.

## The command line

`render.py` does the same work without the app. It takes ZIP files, or folders of them:

```bash
python render.py "Tile 11.zip"                # writes "Tile 11.wmv" next to the ZIP
python render.py path/to/folder               # every ZIP in the folder
python render.py --preview path/to/folder     # one still (.png) per ZIP instead of a video
python render.py --add-to-zip path/to/folder  # also put each video inside its ZIP
python render.py --out videos path/to/folder  # save the output somewhere else
```

## How it works

The script reads the model straight out of the ZIP without unpacking it. If a ZIP holds several OBJ files, as Pedestal 3D downloads often do with low, medium and high detail copies, it renders the largest one that has a material file.

Rendering uses OpenGL through [moderngl](https://github.com/moderngl/moderngl), with no lighting, because the scan textures already have lighting baked in. The model spins around its vertical axis. A model that was saved lying on its side will spin on its side, which is what the preview is for. The frames go straight into ffmpeg, which encodes them as WMV8. [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) ships its own copy of ffmpeg, so you don't need to install it.

Videos are written under a temporary name and renamed when finished, so a cancelled or crashed render never leaves a broken video behind. Adding a video to a ZIP works the same way: the app writes a new copy of the ZIP and swaps it in at the end.

`render.swift` is an earlier macOS-only version using SceneKit. It compiles, but it renders models without their textures.

## Streamlit Community Cloud

Deploy `outputs/tile11-sample/app.py` with Python 3.12 or 3.13. The adjacent `requirements.txt` installs the Python dependencies. The `packages.txt` at the repository root installs the Linux graphics libraries for headless rendering with EGL. Community Cloud requires this file at the root, even when the app is in a subdirectory. See [Streamlit's dependency documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

The default interface uses browser uploads and downloads, so no tkinter or desktop file dialog is needed. Each browser session has its own job and temporary workspace. Uploaded originals on your computer stay unchanged.

Cloud rendering uses software OpenGL and can be slow or run out of memory with large models. Start with one ZIP and use **Preview** before making a video. Streamlit's default upload limit is 200 MB per file. To change it on Community Cloud, set `[server]` and `maxUploadSize` in a `.streamlit/config.toml` at the repository root. Larger uploads also need more memory during model loading and downloads.

## Models

This repository contains no model data. Models downloaded from Pedestal 3D come with their own licences. Check each one before sharing its video.
