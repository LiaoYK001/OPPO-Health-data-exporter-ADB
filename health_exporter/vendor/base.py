from __future__ import annotations

from abc import ABC, abstractmethod
import datetime as dt
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

    @abstractmethod
    def normalize(self, normalizer: Any) -> dict[str, Any]:
        """Finalize vendor rows into the cross-vendor canonical data contract."""
        raise NotImplementedError

    @abstractmethod
    def capture(
        self,
        collector: Any,
        metric: str,
        *,
        earliest_date: dt.date,
        target_dates: tuple[dt.date, ...] | None,
        max_days: int | None,
        resume: bool,
        capture_subpages: bool,
        max_scrolls: int,
    ) -> dict[str, Any]:
        """Capture one canonical metric through the vendor UI state machine."""
        raise NotImplementedError

    @abstractmethod
    def verify(
        self,
        verifier: Any,
        *,
        source_id: int | None,
        verify_all_artifacts: bool,
    ) -> dict[str, Any]:
        """Run vendor-aware checks on top of the shared verifier."""
        raise NotImplementedError
