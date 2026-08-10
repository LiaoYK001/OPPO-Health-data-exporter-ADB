from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


SENSITIVE_COMPONENTS = {
    ".diagnostics",
    ".git",
    "artifacts",
    "data",
    "output",
    "private",
    "raw",
}
SENSITIVE_SUFFIXES = {
    ".apk",
    ".csv",
    ".db",
    ".db-shm",
    ".db-wal",
    ".jks",
    ".jsonl",
    ".key",
    ".keystore",
    ".p12",
    ".passphrase",
    ".pem",
    ".pfx",
    ".shm",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".wal",
    ".xls",
    ".xlsx",
}
REQUIRED_LICENSE_BASENAMES = {
    "Apache-ECharts-LICENSE.txt",
    "Apache-ECharts-NOTICE.txt",
    "LICENSE",
    "Lucide-ISC.txt",
    "React-MIT.txt",
    "THIRD_PARTY_NOTICES.md",
    "d3-BSD-3-Clause.txt",
    "zrender-BSD-3-Clause.txt",
}
FORBIDDEN_EXACT_PATHS = {"document/chat-gemini-before.md"}


def findings_for_names(archive_name: str, names: Iterable[str]) -> list[str]:
    findings: list[str] = []
    normalized_names: list[str] = []
    basenames: set[str] = set()
    for raw_name in names:
        normalized = raw_name.replace("\\", "/")
        normalized_names.append(normalized)
        path = PurePosixPath(normalized)
        basenames.add(path.name)
        lowered_parts = {part.lower() for part in path.parts}
        lowered_name = path.name.lower()
        if (
            path.is_absolute()
            or re.match(r"^[A-Za-z]:", raw_name) is not None
            or ".." in path.parts
        ):
            findings.append(f"{archive_name}: unsafe-member-path: {raw_name}")
        if lowered_parts & SENSITIVE_COMPONENTS:
            findings.append(f"{archive_name}: sensitive-component: {normalized}")
        if any(lowered_name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
            findings.append(f"{archive_name}: sensitive-suffix: {normalized}")
        if any(
            normalized == forbidden or normalized.endswith("/" + forbidden)
            for forbidden in FORBIDDEN_EXACT_PATHS
        ):
            findings.append(f"{archive_name}: forbidden-private-document: {normalized}")

    for basename in sorted(REQUIRED_LICENSE_BASENAMES - basenames):
        findings.append(f"{archive_name}: missing-license-file: {basename}")
    if not normalized_names:
        findings.append(f"{archive_name}: empty-archive")
    return findings


def _archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    raise ValueError(f"不支持的发行包格式：{path.name}")


def scan_distribution(directory: Path) -> list[str]:
    findings: list[str] = []
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if not wheels:
        findings.append("distribution: missing-wheel")
    if not sdists:
        findings.append("distribution: missing-sdist")
    for archive in [*wheels, *sdists]:
        try:
            names = _archive_names(archive)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
            findings.append(f"{archive.name}: unreadable-archive: {type(exc).__name__}")
            continue
        findings.extend(findings_for_names(archive.name, names))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 wheel/sdist 是否夹带本地数据并包含全部许可证文件。"
    )
    parser.add_argument("directory", nargs="?", default="dist", type=Path)
    args = parser.parse_args()
    findings = sorted(set(scan_distribution(args.directory)))
    if findings:
        print("发行包内容检查失败：", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("发行包内容检查通过：未发现私有数据路径，许可证文件完整。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
