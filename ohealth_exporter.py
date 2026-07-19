#!/usr/bin/env python3
"""MuMu + ADB diagnostics and first-stage OHealth data probes.

The first milestone intentionally depends only on the Python standard library.
It reads the Android accessibility hierarchy before falling back to OCR.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


PACKAGE = "com.heytap.health"
DEFAULT_SERIAL = "127.0.0.1:7555"
REMOTE_XML = "/sdcard/ohealth-exporter-window.xml"
REMOTE_PNG = "/sdcard/ohealth-exporter-screen.png"


class ExporterError(RuntimeError):
    pass


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
    raise ExporterError(
        "找不到 adb。请用 --adb 指定路径，或设置 ADB_PATH 环境变量。"
    )


@dataclass
class Adb:
    executable: Path
    serial: str

    def run(
        self,
        *args: str,
        check: bool = True,
        use_serial: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.executable)]
        if use_serial:
            command += ["-s", self.serial]
        command += list(args)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ExporterError(f"ADB 命令失败：{' '.join(command)}\n{detail}")
        return result

    def connect(self) -> None:
        result = self.run("connect", self.serial, use_serial=False, check=False)
        message = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or "failed" in message or "refused" in message:
            raise ExporterError(
                f"无法连接 {self.serial}。请确认 MuMu 已运行并开启 ADB 本地调试。\n"
                f"{(result.stdout + result.stderr).strip()}"
            )

    def shell(self, *args: str, check: bool = True) -> str:
        return self.run("shell", *args, check=check).stdout.strip()

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", remote, str(local))


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def dump_ui(adb: Adb, destination: Path) -> ET.Element:
    adb.shell("uiautomator", "dump", REMOTE_XML)
    adb.pull(REMOTE_XML, destination)
    try:
        return ET.parse(destination).getroot()
    except ET.ParseError as exc:
        raise ExporterError(f"UI XML 解析失败：{destination}") from exc


def capture_screen(adb: Adb, destination: Path) -> None:
    adb.shell("screencap", "-p", REMOTE_PNG)
    adb.pull(REMOTE_PNG, destination)


def foreground(adb: Adb) -> str:
    output = adb.shell("dumpsys", "window")
    match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/([\w.$]+)", output)
    return f"{match.group(1)}/{match.group(2)}" if match else "未知"


def nodes(root: ET.Element) -> list[ET.Element]:
    return list(root.iter("node"))


def local_id(node: ET.Element) -> str:
    return node.attrib.get("resource-id", "").split(":id/")[-1]


def first_text_by_id(root: ET.Element, resource_name: str) -> str | None:
    for node in nodes(root):
        if local_id(node) == resource_name:
            value = node.attrib.get("text", "").strip()
            if value:
                return value
    return None


def visible_labels(root: ET.Element) -> list[str]:
    result: list[str] = []
    for node in nodes(root):
        value = (
            node.attrib.get("text", "").strip()
            or node.attrib.get("content-desc", "").strip()
        )
        if value and value not in result:
            result.append(value)
    return result


def doctor(adb: Adb) -> int:
    adb.connect()
    devices = adb.run("devices", "-l", use_serial=False).stdout.strip()
    size = adb.shell("wm", "size")
    density = adb.shell("wm", "density")
    android = adb.shell("getprop", "ro.build.version.release")
    installed = PACKAGE in adb.shell("pm", "list", "packages", PACKAGE)
    current = foreground(adb)

    with tempfile.TemporaryDirectory(prefix="ohealth-doctor-") as temp_dir:
        root = dump_ui(adb, Path(temp_dir) / "window.xml")
        label_count = len(visible_labels(root))

    checks = [
        ("ADB 设备", adb.serial in devices, adb.serial),
        ("Android", bool(android), android or "未知"),
        ("屏幕", "1440x2560" in size, f"{size}; {density}"),
        ("欢太健康", installed, "已安装" if installed else "未安装"),
        ("前台应用", current.startswith(PACKAGE + "/"), current),
        ("UI 可读", label_count > 0, f"读取到 {label_count} 个可见文本"),
    ]

    print("MuMu / 欢太健康环境检查")
    for name, ok, detail in checks:
        print(f"  {'[OK]' if ok else '[!!]'} {name}: {detail}")

    required_ok = checks[0][1] and installed and checks[-1][1]
    if not required_ok:
        print("\n环境尚未就绪，请先处理 [!!] 项。", file=sys.stderr)
        return 2
    if not checks[2][1]:
        print("\n提示：当前不是基准分辨率 1440x2560，后续坐标兜底需重新标定。")
    if not checks[4][1]:
        print("\n提示：请把欢太健康切到前台后再运行 capture/probe-heart。")
    return 0


def capture(adb: Adb, output_dir: Path) -> int:
    adb.connect()
    run_dir = output_dir / timestamp()
    root = dump_ui(adb, run_dir / "window.xml")
    capture_screen(adb, run_dir / "screen.png")
    labels = visible_labels(root)
    (run_dir / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已保存现场快照：{run_dir.resolve()}")
    print(f"可见文本：{len(labels)} 项")
    return 0


def extract_heart_rate(root: ET.Element) -> dict[str, object]:
    labels = visible_labels(root)
    if "心率" not in labels or first_text_by_id(root, "tv_date") is None:
        raise ExporterError("当前不是心率详情页。请从健康首页点击“心率”卡片。")

    details: dict[str, str] = {}
    for parent in nodes(root):
        title = None
        status = None
        for child in parent.iter("node"):
            if local_id(child) == "tv_title" and child.attrib.get("text", "").strip():
                title = child.attrib["text"].strip()
            elif local_id(child) == "tv_status" and child.attrib.get("text", "").strip():
                status = child.attrib["text"].strip()
        if title and status:
            details[title] = status

    return {
        "schema_version": 1,
        "metric": "heart_rate",
        "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "date_label": first_text_by_id(root, "tv_date"),
        "range_bpm": first_text_by_id(root, "tv_heart_rate_range"),
        "details": details,
    }


def probe_heart(adb: Adb, output_dir: Path) -> int:
    adb.connect()
    if not foreground(adb).startswith(PACKAGE + "/"):
        raise ExporterError("欢太健康当前不在前台。")
    run_dir = output_dir / "heart_rate" / timestamp()
    root = dump_ui(adb, run_dir / "window.xml")
    capture_screen(adb, run_dir / "screen.png")
    data = extract_heart_rate(root)
    result_path = run_dir / "record.json"
    result_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"心率页探针成功：{result_path.resolve()}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 MuMu + ADB 导出欢太健康可见数据（第一阶段）"
    )
    parser.add_argument("--adb", help="adb.exe 路径；默认自动寻找 MuMu 自带 ADB")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="ADB 设备序列号")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="检查 MuMu、ADB、分辨率、APP 与 UI 可读性")

    capture_parser = subparsers.add_parser("capture", help="保存当前屏幕与 UI 树")
    capture_parser.add_argument("--out", type=Path, default=Path("output/captures"))

    heart_parser = subparsers.add_parser(
        "probe-heart", help="在当前心率详情页提取一条样本"
    )
    heart_parser.add_argument("--out", type=Path, default=Path("output"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        adb = Adb(find_adb(args.adb), args.serial)
        if args.command == "doctor":
            return doctor(adb)
        if args.command == "capture":
            return capture(adb, args.out)
        if args.command == "probe-heart":
            return probe_heart(adb, args.out)
        parser.error(f"未知命令：{args.command}")
    except (ExporterError, subprocess.TimeoutExpired) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
