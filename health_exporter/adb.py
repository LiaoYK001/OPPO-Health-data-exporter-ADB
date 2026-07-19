from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_SERIAL
from .errors import ExporterError, RootRequiredError


def find_adb(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.environ.get("ADB_PATH"),
        shutil.which("adb"),
        r"C:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe",
        r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise ExporterError("找不到 adb.exe；请使用 --adb 指定 MuMu 自带 ADB。")


@dataclass
class AdbClient:
    executable: Path
    serial: str = DEFAULT_SERIAL

    def run(
        self,
        *args: str,
        check: bool = True,
        use_serial: bool = True,
        timeout: int = 60,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
        command = [str(self.executable)]
        if use_serial:
            command += ["-s", self.serial]
        command += [str(arg) for arg in args]
        kwargs: dict[str, object] = {
            "capture_output": True,
            "timeout": timeout,
        }
        if not binary:
            kwargs.update(text=True, encoding="utf-8", errors="replace")
        result = subprocess.run(command, **kwargs)
        if check and result.returncode != 0:
            stdout = result.stdout.decode(errors="replace") if binary else result.stdout
            stderr = result.stderr.decode(errors="replace") if binary else result.stderr
            detail = (stderr or stdout or "").strip()
            raise ExporterError(f"ADB 命令失败：{' '.join(command)}\n{detail}")
        return result

    def connect(self) -> None:
        result = self.run("connect", self.serial, check=False, use_serial=False)
        message = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or "failed" in message or "refused" in message:
            raise ExporterError(f"无法连接 MuMu ADB {self.serial}：{message.strip()}")

    def shell(self, *args: str, check: bool = True, timeout: int = 60) -> str:
        return self.run("shell", *args, check=check, timeout=timeout).stdout.strip()

    def su(self, script: str, *, check: bool = True, timeout: int = 120) -> str:
        remote_command = f"su -c {shlex.quote(script)}"
        result = self.run("shell", remote_command, check=False, timeout=timeout)
        output = f"{result.stdout}\n{result.stderr}".strip()
        if check and result.returncode != 0:
            if "inaccessible" in output or "not found" in output:
                raise RootRequiredError(
                    "MuMu 尚未开启 Root。请在设置中心 → 其他设置开启 Root 后重启。"
                )
            raise ExporterError(f"Root 命令失败：{output}")
        return result.stdout.strip()

    def root_available(self) -> bool:
        result = self.run("shell", "su", "-c", "id", check=False, timeout=10)
        return result.returncode == 0 and "uid=0" in result.stdout

    def pull(self, remote: str, local: Path, *, timeout: int = 180) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", remote, str(local), timeout=timeout)

    def pull_root_file(self, remote: str, local: Path, token: str) -> None:
        stage = f"/sdcard/Download/ohealth-exporter-{token}"
        quoted_remote = shlex.quote(remote)
        quoted_stage = shlex.quote(stage)
        try:
            self.su(
                f"cp {quoted_remote} {quoted_stage} && chmod 0644 {quoted_stage}",
                timeout=180,
            )
            self.pull(stage, local, timeout=300)
        finally:
            self.shell("rm", "-f", stage, check=False)

    def package_version(self, package: str) -> str | None:
        output = self.shell("dumpsys", "package", package)
        match = re.search(r"versionName=([^\s]+)", output)
        return match.group(1) if match else None

    def foreground(self) -> str:
        output = self.shell("dumpsys", "window")
        match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/([\w.$]+)", output)
        return f"{match.group(1)}/{match.group(2)}" if match else "unknown"

    def start_activity(self, component: str) -> None:
        result = self.run(
            "shell", "am", "start", "-n", component, check=False, timeout=30
        )
        if result.returncode == 0 and "SecurityException" not in result.stdout:
            return
        package = component.split("/", 1)[0]
        self.shell(
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )

    def force_stop(self, package: str) -> None:
        self.shell("am", "force-stop", package)

    def keyevent(self, key: str) -> None:
        self.shell("input", "keyevent", key)

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 350) -> None:
        self.shell(
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        )

    @staticmethod
    def quote(value: str) -> str:
        return shlex.quote(value)
