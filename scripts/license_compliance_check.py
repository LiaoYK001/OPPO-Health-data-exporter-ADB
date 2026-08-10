from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_LICENSE_MARKERS = {
    "defusedxml": ("psfl", "python software foundation license"),
    "fastapi": ("mit",),
    "numpy": ("bsd license", "redistribution and use in source and binary forms"),
    "openpyxl": ("mit",),
    "pillow": ("mit-cmu",),
    "uvicorn": ("bsd-3-clause", "bsd license"),
}
OPTIONAL_PYTHON_LICENSE_MARKERS = {
    "frida": ("wxwindows library licence",),
}
NODE_LICENSES = {
    "echarts": "Apache-2.0",
    "lucide-react": "ISC",
    "react": "MIT",
    "react-dom": "MIT",
    "zrender": "BSD-3-Clause",
}
REQUIRED_NOTICE_FILES = (
    "THIRD_PARTY_NOTICES.md",
    "third_party/Apache-ECharts-LICENSE.txt",
    "third_party/Apache-ECharts-NOTICE.txt",
    "third_party/Lucide-ISC.txt",
    "third_party/React-MIT.txt",
    "third_party/d3-BSD-3-Clause.txt",
    "third_party/zrender-BSD-3-Clause.txt",
)


def _dependency_name(requirement: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", requirement)
    if not match:
        raise ValueError(f"无法解析 Python 依赖声明：{requirement!r}")
    return match.group(1).lower().replace("_", "-")


def _python_license_text(package: str) -> str:
    package_metadata = metadata(package)
    fields = [
        package_metadata.get("License-Expression") or "",
        package_metadata.get("License") or "",
        *(package_metadata.get_all("Classifier") or []),
    ]
    return "\n".join(fields).lower()


def _check_python(findings: list[str]) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        _dependency_name(requirement)
        for requirement in project["project"].get("dependencies", [])
    }
    expected = set(PYTHON_LICENSE_MARKERS)
    if declared != expected:
        findings.append(
            "python-runtime-dependency-set: pyproject dependencies changed without license review"
        )
    for package, markers in PYTHON_LICENSE_MARKERS.items():
        try:
            license_text = _python_license_text(package)
        except PackageNotFoundError:
            findings.append(f"python-package-missing: {package}")
            continue
        if not any(marker in license_text for marker in markers):
            findings.append(f"python-license-unexpected: {package}")
    for package, markers in OPTIONAL_PYTHON_LICENSE_MARKERS.items():
        try:
            license_text = _python_license_text(package)
        except PackageNotFoundError:
            continue
        if not any(marker in license_text for marker in markers):
            findings.append(f"python-optional-license-unexpected: {package}")


def _check_node(findings: list[str]) -> None:
    package = json.loads((ROOT / "webapp" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "webapp" / "package-lock.json").read_text(encoding="utf-8"))
    direct = set(package.get("dependencies", {}))
    expected_direct = set(NODE_LICENSES) - {"zrender"}
    if direct != expected_direct:
        findings.append(
            "node-runtime-dependency-set: package dependencies changed without license review"
        )
    packages = lock.get("packages", {})
    for name, expected_license in NODE_LICENSES.items():
        entry = packages.get(f"node_modules/{name}")
        if not isinstance(entry, dict):
            findings.append(f"node-package-missing-from-lock: {name}")
            continue
        if entry.get("license") != expected_license:
            findings.append(f"node-license-unexpected: {name}")


def main() -> int:
    findings: list[str] = []
    _check_python(findings)
    _check_node(findings)
    for relative in REQUIRED_NOTICE_FILES:
        if not (ROOT / relative).is_file():
            findings.append(f"third-party-notice-missing: {relative}")
    if findings:
        print("第三方许可检查失败：", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("第三方许可检查通过：运行时依赖和归属文件与审核清单一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
