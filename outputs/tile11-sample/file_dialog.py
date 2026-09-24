"""Native "choose files" and "choose folder" dialogs for app.py.

The app runs on the same computer as the browser, so it can open the operating system's own dialog and get real
paths back. Each dialog runs in a separate process, because GUI toolkits refuse to open windows from Streamlit's
worker threads.

macOS uses AppleScript, which every Mac has (Homebrew's Python doesn't include tkinter). Linux tries zenity and
kdialog before tkinter. Windows uses tkinter, which the python.org installer includes.
"""
import shutil
import subprocess
import sys
from pathlib import Path


class DialogUnavailable(Exception):
    pass


def choose_files(start):
    """Ask for one or more ZIP or GLB files. Returns a list of paths, empty if the dialog was cancelled."""
    return [Path(p) for p in _run("files", _existing_dir(start))]


def choose_folder(start):
    """Ask for a folder. Returns its path, or None if the dialog was cancelled."""
    picked = _run("folder", _existing_dir(start))
    return Path(picked[0]) if picked else None


def _existing_dir(path):
    path = Path(path).expanduser()
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return str(path if path.is_dir() else Path.home())


def _run(kind, start):
    if sys.platform == "darwin":
        return _applescript(kind, start)
    if sys.platform.startswith("linux"):
        if shutil.which("zenity"):
            return _command(_zenity(kind, start), cancel_codes=(1,))
        if shutil.which("kdialog"):
            return _command(_kdialog(kind, start), cancel_codes=(1,))
    return _tkinter(kind, start)


APPLESCRIPT = """
on run argv
    set startFolder to POSIX file (item 2 of argv)
    activate
    if item 1 of argv is "files" then
        set picked to choose file with prompt "Choose ZIP or GLB files" of type {"zip", "glb"} default location startFolder with multiple selections allowed
    else
        set picked to {choose folder with prompt "Choose a folder of ZIP or GLB files" default location startFolder}
    end if
    set output to ""
    repeat with chosen in picked
        set output to output & POSIX path of chosen & linefeed
    end repeat
    return output
end run
"""


def _applescript(kind, start):
    result = subprocess.run(["osascript", "-e", APPLESCRIPT, kind, start], capture_output=True, text=True)
    if result.returncode != 0:
        if "-128" in result.stderr:  # the user clicked Cancel
            return []
        raise DialogUnavailable(result.stderr.strip() or "osascript failed")
    return _lines(result.stdout)


def _zenity(kind, start):
    if kind == "files":
        return ["zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Choose ZIP or GLB files",
                "--file-filter=ZIP and GLB files | *.zip *.ZIP *.glb *.GLB", f"--filename={start}/"]
    return ["zenity", "--file-selection", "--directory", "--title=Choose a folder of ZIP or GLB files",
            f"--filename={start}/"]


def _kdialog(kind, start):
    if kind == "files":
        return ["kdialog", "--getopenfilename", start, "*.zip *.ZIP *.glb *.GLB|ZIP and GLB files", "--multiple",
                "--separate-output"]
    return ["kdialog", "--getexistingdirectory", start]


def _command(args, cancel_codes):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode in cancel_codes:
        return []
    if result.returncode != 0:
        raise DialogUnavailable(result.stderr.strip() or f"{args[0]} failed")
    return _lines(result.stdout)


TKINTER = """
import sys
import tkinter
from tkinter import filedialog

root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
if sys.argv[1] == "files":
    picked = filedialog.askopenfilenames(parent=root, title="Choose ZIP or GLB files", initialdir=sys.argv[2],
                                         filetypes=[("ZIP and GLB files", "*.zip *.glb"), ("All files", "*")])
else:
    folder = filedialog.askdirectory(parent=root, title="Choose a folder of ZIP or GLB files", initialdir=sys.argv[2])
    picked = [folder] if folder else []
print("\\n".join(picked))
"""


def _tkinter(kind, start):
    result = subprocess.run([sys.executable, "-c", TKINTER, kind, start], capture_output=True, text=True)
    if result.returncode != 0:
        if "tkinter" in result.stderr:
            raise DialogUnavailable("this Python has no tkinter, so it can't open a file dialog")
        raise DialogUnavailable(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "tkinter failed")
    return _lines(result.stdout)


def _lines(output):
    return [line for line in output.splitlines() if line.strip()]
