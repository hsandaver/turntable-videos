"""Keep uploaded ZIP and GLB files and their outputs in a temporary workspace for one session."""
import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4


class UploadWorkspace:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="turntable-")
        self.paths = {}

    def save(self, uploaded):
        # Browsers usually send a basename, but never trust client-provided paths.
        name = Path(uploaded.name.replace("\\", "/")).name
        if Path(name).suffix.lower() not in (".zip", ".glb"):
            raise ValueError("Please upload ZIP or GLB files.")
        data = uploaded.getbuffer()
        key = (name, hashlib.sha256(data).hexdigest())
        if key not in self.paths:
            folder = Path(self.directory.name) / uuid4().hex
            folder.mkdir()
            path = folder / name
            path.write_bytes(data)
            self.paths[key] = path
        # Don't rewrite it on reruns: rendering may have added a video to this ZIP.
        return self.paths[key]
