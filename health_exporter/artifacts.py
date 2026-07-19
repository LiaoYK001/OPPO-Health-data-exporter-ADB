from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .db import Database


@dataclass(frozen=True)
class Artifact:
    sha256: str
    path: Path
    relative_path: str
    media_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None


class ArtifactStore:
    def __init__(self, data_dir: Path, db: Database):
        self.data_dir = data_dir
        self.root = data_dir / "artifacts" / "sha256"
        self.db = db
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def put_file(
        self,
        source: Path,
        *,
        media_type: str | None = None,
        suffix: str | None = None,
    ) -> Artifact:
        digest = self.digest_file(source)
        extension = suffix or source.suffix.lower() or ".bin"
        destination = self.root / digest[:2] / f"{digest}{extension}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(source, destination)
        detected_type = media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        width = height = None
        if detected_type.startswith("image/"):
            try:
                with Image.open(destination) as image:
                    width, height = image.size
            except Exception:
                pass
        relative = destination.relative_to(self.data_dir).as_posix()
        artifact = Artifact(
            digest,
            destination,
            relative,
            detected_type,
            destination.stat().st_size,
            width,
            height,
        )
        self.db.add_artifact(
            artifact.sha256,
            artifact.relative_path,
            artifact.media_type,
            artifact.byte_size,
            artifact.width,
            artifact.height,
        )
        return artifact

    def put_bytes(
        self, content: bytes, *, suffix: str, media_type: str
    ) -> Artifact:
        digest = hashlib.sha256(content).hexdigest()
        destination = self.root / digest[:2] / f"{digest}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(content)
        return self.put_file(destination, media_type=media_type, suffix=suffix)

    def create_view_link(self, artifact: Artifact, view_path: Path) -> Path:
        view_path.parent.mkdir(parents=True, exist_ok=True)
        if view_path.exists():
            if self.digest_file(view_path) == artifact.sha256:
                return view_path
            view_path.unlink()
        try:
            os.link(artifact.path, view_path)
        except OSError:
            shutil.copyfile(artifact.path, view_path)
        return view_path
