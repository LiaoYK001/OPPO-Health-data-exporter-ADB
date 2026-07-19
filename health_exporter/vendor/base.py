from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SnapshotFile:
    remote_path: str
    local_path: Path
    artifact_sha256: str
    sqlite_status: str
    tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotResult:
    run_id: str
    files: tuple[SnapshotFile, ...]
    manifest_path: Path


class VendorAdapter(ABC):
    vendor: str

    @abstractmethod
    def discover(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, run_id: str) -> SnapshotResult:
        raise NotImplementedError

    @abstractmethod
    def extract(self, snapshot: SnapshotResult, source_id: int, run_id: str) -> dict[str, Any]:
        raise NotImplementedError
