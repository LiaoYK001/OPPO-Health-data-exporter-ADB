from __future__ import annotations

import io
import lzma

import pytest

from health_exporter.artifacts import ArtifactStore
from health_exporter.db import Database
from health_exporter.errors import ExporterError
from health_exporter.frida_bridge import (
    _decompress_limited,
    _read_limited,
    _validate_github_url,
)
from health_exporter.vendor.oppo import APP_DATA_ROOT, _safe_relative


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, content_length: str | None = None):
        super().__init__(payload)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length


def test_frida_download_urls_are_restricted_to_official_github_release() -> None:
    _validate_github_url("https://api.github.com/repos/frida/frida/releases/tags/17.0.0")
    _validate_github_url(
        "https://github.com/frida/frida/releases/download/17.0.0/server.xz",
        asset=True,
    )

    with pytest.raises(ExporterError):
        _validate_github_url("file:///tmp/server.xz", asset=True)
    with pytest.raises(ExporterError):
        _validate_github_url("https://example.invalid/server.xz", asset=True)
    with pytest.raises(ExporterError):
        _validate_github_url(
            "https://github.com/unrelated/project/releases/download/1/file.xz",
            asset=True,
        )


def test_frida_download_size_is_bounded() -> None:
    assert _read_limited(_Response(b"abc", "3"), 3, "fixture") == b"abc"
    with pytest.raises(ExporterError):
        _read_limited(_Response(b"abcd"), 3, "fixture")
    with pytest.raises(ExporterError):
        _read_limited(_Response(b"", "4"), 3, "fixture")


def test_frida_decompression_is_bounded() -> None:
    compressed = lzma.compress(b"abcd")
    assert _decompress_limited(compressed, 4) == b"abcd"
    with pytest.raises(ExporterError):
        _decompress_limited(compressed, 3)


def test_oppo_snapshot_paths_must_stay_inside_app_data() -> None:
    assert _safe_relative(f"{APP_DATA_ROOT}/databases/database.db").as_posix() == (
        "databases/database.db"
    )
    with pytest.raises(ExporterError):
        _safe_relative("/data/local/tmp/database.db")
    with pytest.raises(ExporterError):
        _safe_relative(f"{APP_DATA_ROOT}/databases/../outside.db")


def test_artifact_paths_and_existing_hashes_are_enforced(tmp_path) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "health.sqlite3", data_dir / "private")
    store = ArtifactStore(data_dir, database)
    artifact = store.put_bytes(b"synthetic", suffix=".bin", media_type="application/octet-stream")

    with pytest.raises(ExporterError):
        store.put_bytes(b"value", suffix="../../outside", media_type="application/octet-stream")
    with pytest.raises(ExporterError):
        store.create_view_link(artifact, tmp_path / "outside" / "evidence.bin")

    artifact.path.write_bytes(b"tampered")
    with pytest.raises(ExporterError):
        store.put_bytes(b"synthetic", suffix=".bin", media_type="application/octet-stream")
    database.close()
