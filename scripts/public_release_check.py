from __future__ import annotations

import argparse
import os
import re
import shutil
# Fixed argv only; shell execution is never used.
import subprocess  # nosec B404
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable

from PIL import ExifTags, Image, UnidentifiedImageError


ROOT = Path(__file__).resolve().parents[1]
GIT_EXECUTABLE = shutil.which("git")
if GIT_EXECUTABLE is None:  # pragma: no cover - the checker itself requires Git.
    raise RuntimeError("Git executable was not found on PATH")
SENSITIVE_SUFFIXES = {
    ".apk",
    ".csv",
    ".db",
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
    ".wal",
    ".xls",
    ".xlsx",
}
SENSITIVE_COMPONENTS = {
    ".diagnostics",
    ".env",
    "artifacts",
    "data",
    "output",
    "private",
    "raw",
}
SENSITIVE_NAMES = {"id_salt.bin", "web-auth.json"}
RASTER_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
REVIEWED_PUBLIC_IMAGES = {
    "health_exporter/web_dist/og.png",
    "webapp/public/og.png",
}
GITHUB_NOREPLY_SUFFIX = "@" + "users.noreply.github.com"
GITHUB_NOREPLY_ADDRESS = "noreply" + "@github.com"
PUBLIC_AUTHOR_NAMES = {
    "LYKK231",
    "LiaoYK001",
    "dependabot[bot]",
    "github-actions[bot]",
}
IGNORED_SENTINELS = (
    ".env",
    ".diagnostics/calendar-proof/example.png",
    "data/health.sqlite3",
    "data/private/id_salt.bin",
    "data/private/oppo-database.passphrase",
    "data/artifacts/example.png",
    "document/chat-gemini-before.md",
    "output/health_daily.xlsx",
)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Trusted Git path and fixed internal argv.
    return subprocess.run(  # nosec B603
        [GIT_EXECUTABLE, *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _publishable_files(*, tracked_only: bool = False) -> list[str]:
    # Trusted Git path and fixed argv.
    arguments = [GIT_EXECUTABLE, "ls-files", "-z", "--cached"]
    if not tracked_only:
        arguments.extend(["--others", "--exclude-standard"])
    result = subprocess.run(  # nosec B603
        arguments,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]


def _sensitive_path(path: str) -> bool:
    pure = PurePosixPath(path.replace("\\", "/"))
    lowered = [part.lower() for part in pure.parts]
    name = pure.name.lower()
    if name in SENSITIVE_NAMES or any(part in SENSITIVE_COMPONENTS for part in lowered):
        return True
    if any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return True
    return name.endswith((".db-wal", ".db-shm", ".sqlite3-wal", ".sqlite3-shm"))


def _text_rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return (
        ("windows-user-home", re.compile(r"(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+")),
        ("posix-user-home", re.compile(r"/(?:home|Users)/[^/\s\"']+")),
        ("email-address", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
        ("mainland-phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
        (
            "mainland-id",
            re.compile(
                r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
            ),
        ),
        ("hardware-address", re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")),
        ("private-key", re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
        ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
        ("openai-token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
        ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
        ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        (
            "credential-literal",
            re.compile(
                r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passphrase)"
                r"\s*[:=]\s*[\"']([^\"']{16,})[\"']"
            ),
        ),
    )


def _image_metadata(path: Path) -> str:
    values: list[str] = []
    try:
        with Image.open(path) as image:
            for key, value in image.info.items():
                if isinstance(value, str):
                    values.append(f"{key}={value}")
                elif isinstance(value, bytes) and any(
                    marker in key.lower()
                    for marker in ("comment", "description", "prompt", "software", "xmp")
                ):
                    values.append(f"{key}={value[: 1024 * 1024].decode('utf-8', errors='replace')}")
            for tag, value in image.getexif().items():
                if isinstance(value, str):
                    values.append(f"{ExifTags.TAGS.get(tag, tag)}={value}")
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("tracked raster image metadata is unreadable") from exc
    return "\n".join(values)


def _scan_current(findings: list[str], *, tracked_only: bool = False) -> None:
    rules = _text_rules()
    for relative in _publishable_files(tracked_only=tracked_only):
        if _sensitive_path(relative):
            findings.append(f"publishable-sensitive-path: {relative}")
        path = ROOT / relative
        try:
            payload = path.read_bytes()
        except OSError:
            findings.append(f"publishable-file-unreadable: {relative}")
            continue
        suffix = path.suffix.lower()
        if suffix in RASTER_IMAGE_SUFFIXES:
            if relative.replace("\\", "/") not in REVIEWED_PUBLIC_IMAGES:
                findings.append(f"unreviewed-public-image: {relative}")
            try:
                metadata = _image_metadata(path)
            except ValueError:
                findings.append(f"image-metadata-unreadable: {relative}")
            else:
                for rule_name, pattern in rules:
                    if pattern.search(metadata):
                        findings.append(f"image-metadata-{rule_name}: {relative}")
        if b"\0" in payload[:8192]:
            continue
        text = payload.decode("utf-8", errors="replace")
        for rule_name, pattern in rules:
            if relative == "scripts/public_release_check.py" and rule_name == "credential-literal":
                continue
            if pattern.search(text):
                findings.append(f"{rule_name}: {relative}")

    for sentinel in IGNORED_SENTINELS:
        result = _git("check-ignore", "-q", "--no-index", sentinel, check=False)
        if result.returncode != 0:
            findings.append(f"missing-ignore-rule: {sentinel}")


def _history_paths(findings: list[str]) -> None:
    result = _git("rev-list", "--objects", "--all")
    reported: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split(" ", 1)
        if len(fields) == 2 and _sensitive_path(fields[1]) and fields[1] not in reported:
            findings.append(f"history-sensitive-path: {fields[1]}")
            reported.add(fields[1])


def _history_authors(findings: list[str]) -> None:
    result = _git("log", "--all", "--format=%an%x00%ae%x00%cn%x00%ce")
    entries = [
        fields
        for line in result.stdout.splitlines()
        if len(fields := line.split("\0")) == 4
    ]
    unsafe_emails = [
        author_email.strip()
        for author_name, author_email, committer_name, committer_email in entries
        if author_email.strip()
        and not author_email.strip().lower().endswith(GITHUB_NOREPLY_SUFFIX)
        and author_email.strip().lower() != GITHUB_NOREPLY_ADDRESS
    ]
    unsafe_names = [
        author_name.strip()
        for author_name, author_email, committer_name, committer_email in entries
        if author_name.strip() not in PUBLIC_AUTHOR_NAMES
    ]
    unsafe_committer_emails = [
        committer_email.strip()
        for author_name, author_email, committer_name, committer_email in entries
        if committer_email.strip()
        and not committer_email.strip().lower().endswith(GITHUB_NOREPLY_SUFFIX)
        and committer_email.strip().lower() != GITHUB_NOREPLY_ADDRESS
    ]
    unsafe_committer_names = [
        committer_name.strip()
        for author_name, author_email, committer_name, committer_email in entries
        if committer_name.strip() not in PUBLIC_AUTHOR_NAMES
    ]
    if unsafe_emails:
        findings.append(
            f"history-author-email: {len(unsafe_emails)} commit(s) use a non-noreply address"
        )
    if unsafe_names:
        findings.append(
            f"history-author-name: {len(unsafe_names)} commit(s) use an unapproved public name"
        )
    if unsafe_committer_emails:
        findings.append(
            "history-committer-email: "
            f"{len(unsafe_committer_emails)} commit(s) use a non-noreply address"
        )
    if unsafe_committer_names:
        findings.append(
            "history-committer-name: "
            f"{len(unsafe_committer_names)} commit(s) use an unapproved public name"
        )


def _history_commit_messages(findings: list[str]) -> None:
    rules = _text_rules()
    for revision in _git("rev-list", "--all").stdout.splitlines():
        message = _git("show", "-s", "--format=%B", revision).stdout
        for rule_name, pattern in rules:
            if pattern.search(message):
                findings.append(
                    f"history-commit-message-{rule_name}: {revision[:12]}"
                )


def _history_text(findings: list[str]) -> None:
    patterns = {
        "history-user-home": r"([A-Za-z]:[\\/]+Users[\\/]+|/(home|Users)/[^/[:space:]]+)",
        "history-email-address": (
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
        "history-mainland-phone": r"(^|[^0-9])1[3-9][0-9]{9}([^0-9]|$)",
        "history-mainland-id": (
            r"(^|[^0-9])[0-9]{6}(19|20)[0-9]{2}(0[1-9]|1[0-2])"
            r"(0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx]([^0-9]|$)"
        ),
        "history-hardware-address": (
            r"(^|[^0-9A-Fa-f])([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
            r"([^0-9A-Fa-f]|$)"
        ),
        "history-private-key": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "history-token": r"(gh[pousr]_|github_pat_|sk-(proj-)?|xox[baprs]-|AKIA|ASIA)[A-Za-z0-9_-]{16,}",
    }
    revisions = _git("rev-list", "--all").stdout.splitlines()
    reported: set[tuple[str, str]] = set()
    for revision in revisions:
        for rule_name, pattern in patterns.items():
            result = _git(
                "grep", "-I", "-l", "-E", "-e", pattern, revision, "--", check=False
            )
            if result.returncode not in {0, 1}:
                findings.append(f"history-scan-error: {revision[:12]} ({rule_name})")
                continue
            for line in result.stdout.splitlines():
                _, _, path = line.partition(":")
                key = (rule_name, path)
                if path and key not in reported:
                    findings.append(f"{rule_name}: {path}")
                    reported.add(key)


def _github_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _print_findings(findings: Iterable[str]) -> int:
    unique = sorted(set(findings))
    if not unique:
        print("公开发布检查通过：待发布文件中未发现本地数据、凭据或身份路径。")
        return 0
    print("公开发布检查失败；仅列出规则和文件，不输出疑似秘密内容：", file=sys.stderr)
    for finding in unique:
        print(f"- {finding}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            print(
                f"::error title=Public release check::{_github_escape(finding)}",
                file=sys.stderr,
            )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="检查公开仓库中的敏感路径、PII 和凭据痕迹")
    parser.add_argument(
        "--history",
        action="store_true",
        help="同时检查所有 Git 历史路径、作者邮箱和高风险文本",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="只检查 Git 已跟踪文件；用于干净 checkout 中的 CI",
    )
    args = parser.parse_args()
    findings: list[str] = []
    _scan_current(findings, tracked_only=args.tracked_only)
    if args.history:
        _history_paths(findings)
        _history_authors(findings)
        _history_commit_messages(findings)
        _history_text(findings)
    return _print_findings(findings)


if __name__ == "__main__":
    raise SystemExit(main())
