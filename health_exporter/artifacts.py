from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .db import Database
from .errors import ExporterError


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
        self.data_dir = data_dir.resolve()
        self.root = (self.data_dir / "artifacts" / "sha256").resolve()
        self.db = db
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_extension(value: str) -> str:
        extension = value.lower()
        if (
            not extension.startswith(".")
            or len(extension) > 32
            or Path(extension).name != extension
            or any(character not in ".-_abcdefghijklmnopqrstuvwxyz0123456789" for character in extension)
        ):
            raise ExporterError("素材文件后缀不安全。")
        return extension

    def _destination(self, digest: str, extension: str) -> Path:
        destination = (self.root / digest[:2] / f"{digest}{extension}").resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise ExporterError("素材目标路径超出内容寻址目录。") from exc
        return destination

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
        extension = self._safe_extension(suffix or source.suffix or ".bin")
        destination = self._destination(digest, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self.digest_file(destination) != digest:
                raise ExporterError("已有内容寻址素材的 SHA-256 与文件名不一致。")
        else:
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                if self.digest_file(temporary) != digest:
                    raise ExporterError("素材复制后的 SHA-256 与源文件不一致。")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        detected_type = media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        width = height = None
        if detected_type.startswith("image/"):
            try:
                with Image.open(destination) as image:
                    width, height = image.size
            # Metadata is optional; an unreadable image remains hash-addressed evidence.
            except Exception:  # nosec B110
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
        extension = self._safe_extension(suffix)
        destination = self._destination(digest, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self.digest_file(destination) != digest:
                raise ExporterError("已有内容寻址素材的 SHA-256 与文件名不一致。")
        else:
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                if self.digest_file(temporary) != digest:
                    raise ExporterError("素材写入后的 SHA-256 与内容不一致。")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return self.put_file(destination, media_type=media_type, suffix=extension)

    def create_view_link(self, artifact: Artifact, view_path: Path) -> Path:
        view_path = view_path.resolve()
        try:
            view_path.relative_to(self.data_dir)
            artifact.path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ExporterError("素材视图路径必须位于数据目录内。") from exc
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
