from __future__ import annotations

import hashlib
import json
import lzma
import os
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .adb import AdbClient
from .constants import PACKAGE
from .errors import ExporterError, RootRequiredError


ABI_NAMES = {
    "arm64-v8a": "arm64",
    "armeabi-v7a": "arm",
    "x86_64": "x86_64",
    "x86": "x86",
}


def _frida_module() -> Any:
    try:
        import frida  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ExporterError(
            "缺少可选 Frida 客户端。请先执行：python -m pip install -e \".[frida]\""
        ) from exc
    return frida


def prepare_server(adb: AdbClient, private_dir: Path) -> tuple[Path, str]:
    """Download the server matching the installed client and verify GitHub's SHA-256."""
    frida = _frida_module()
    version = str(frida.__version__)
    android_abi = adb.shell("getprop", "ro.product.cpu.abi")
    abi = ABI_NAMES.get(android_abi)
    if abi is None:
        raise ExporterError(f"Frida 暂不支持该模拟器 ABI：{android_abi}")
    asset_name = f"frida-server-{version}-android-{abi}.xz"
    release_url = f"https://api.github.com/repos/frida/frida/releases/tags/{version}"
    request = urllib.request.Request(release_url, headers={"User-Agent": "ohealth-exporter"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except Exception as exc:
        raise ExporterError(f"无法读取 Frida 官方发布信息：{exc}") from exc
    asset = next((item for item in release.get("assets", []) if item["name"] == asset_name), None)
    if asset is None:
        raise ExporterError(f"Frida 发布 {version} 中没有 {asset_name}")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise ExporterError("Frida 官方发布未提供 SHA-256，拒绝使用未校验二进制。")
    expected = digest.split(":", 1)[1].lower()
    tools_dir = private_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    compressed = tools_dir / asset_name
    server = tools_dir / asset_name.removesuffix(".xz")
    if not server.exists():
        try:
            with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as response:
                payload = response.read()
        except Exception as exc:
            raise ExporterError(f"下载 Frida Server 失败：{exc}") from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ExporterError(
                f"Frida Server SHA-256 不匹配：expected={expected}, actual={actual}"
            )
        compressed.write_bytes(payload)
        server.write_bytes(lzma.decompress(payload))
        compressed.unlink(missing_ok=True)
    return server, version


def capture_database_key(
    adb: AdbClient,
    private_dir: Path,
    plaintext_output: Path,
    timeout: int = 45,
) -> dict[str, Any]:
    """Capture the passphrase and export a temporary plaintext copy through SQLCipher."""
    if not adb.root_available():
        raise RootRequiredError("key-capture 需要短暂开启 MuMu Root 并重启。")
    frida = _frida_module()
    server, version = prepare_server(adb, private_dir)
    remote = f"/data/local/tmp/ohealth-frida-server-{version}"
    adb.run("push", str(server), remote, timeout=180)
    adb.su(f"chmod 0700 {AdbClient.quote(remote)}")
    adb.su("pkill -f ohealth-frida-server", check=False)
    adb.su(
        f"nohup {AdbClient.quote(remote)} >/dev/null 2>&1 </dev/null &",
        timeout=15,
    )
    adb.run("forward", "tcp:27042", "tcp:27042")

    key_found = threading.Event()
    export_finished = threading.Event()
    state: dict[str, Any] = {}
    script_source = r"""
Java.perform(function () {
  const AppDatabase = Java.use('com.heytap.databaseengineservice.db.AppDatabase');
  const initialize = AppDatabase.v.overload('android.content.Context', 'java.lang.String');
  const getDatabase = AppDatabase.K.overload('android.content.Context');
  let exported = false;
  initialize.implementation = function (context, passphrase) {
    send({kind: 'ohealth-db-key', value: String(passphrase)});
    return initialize.call(this, context, passphrase);
  };
  getDatabase.implementation = function (context) {
    const room = getDatabase.call(this, context);
    if (!exported) {
      exported = true;
      const output = context.getCacheDir().getAbsolutePath() + '/ohealth-plaintext-export.db';
      try {
        const File = Java.use('java.io.File');
        File.$new(output).delete();
        const db = room.getOpenHelper().getWritableDatabase();
        db.execSQL("ATTACH DATABASE '" + output + "' AS plaintext KEY ''");
        db.query("SELECT sqlcipher_export('plaintext')").close();
        db.execSQL('DETACH DATABASE plaintext');
        send({kind: 'ohealth-db-export', value: String(output)});
      } catch (error) {
        send({kind: 'ohealth-db-export-error', value: String(error.stack || error)});
      }
    }
    return room;
  };
});
"""

    def on_message(message: dict[str, Any], data: bytes | None) -> None:
        if message.get("type") == "send":
            payload = message.get("payload", {})
            if payload.get("kind") == "ohealth-db-key":
                state["key"] = str(payload["value"])
                key_found.set()
            elif payload.get("kind") == "ohealth-db-export":
                state["remote_plaintext"] = str(payload["value"])
                export_finished.set()
            elif payload.get("kind") == "ohealth-db-export-error":
                state["export_error"] = str(payload["value"])
                export_finished.set()
        elif message.get("type") == "error":
            state["error"] = message.get("stack") or message.get("description")
            key_found.set()
            export_finished.set()

    session = None
    script = None
    try:
        manager = frida.get_device_manager()
        device = manager.add_remote_device("127.0.0.1:27042")
        adb.force_stop(PACKAGE)
        pid = device.spawn([PACKAGE])
        session = device.attach(pid)
        script = session.create_script(script_source)
        script.on("message", on_message)
        script.load()
        device.resume(pid)
        if not key_found.wait(timeout):
            raise ExporterError(
                "等待数据库初始化超时。保持 Root 开启，重新运行 key-capture；"
                "无需在风险提示中点击确认。"
            )
        if "key" not in state:
            raise ExporterError(f"Frida 脚本失败：{state.get('error', 'unknown error')}")
        key_path = private_dir / "oppo-database.passphrase"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(state["key"], encoding="utf-8")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        if not export_finished.wait(timeout):
            raise ExporterError(
                "已安全保存数据库密钥，但等待 SQLCipher 明文副本导出超时；可重新运行 key-capture。"
            )
        if "remote_plaintext" not in state:
            raise ExporterError(
                "已安全保存数据库密钥，但 SQLCipher 明文导出失败："
                f"{state.get('export_error', state.get('error', 'unknown error'))}"
            )
        plaintext_output.parent.mkdir(parents=True, exist_ok=True)
        adb.pull_root_file(
            state["remote_plaintext"], plaintext_output, f"plain-{uuid.uuid4().hex}"
        )
        plaintext_sha256 = hashlib.sha256(plaintext_output.read_bytes()).hexdigest()
        return {
            "key_path": str(key_path.resolve()),
            "key_sha256": hashlib.sha256(state["key"].encode("utf-8")).hexdigest(),
            "plaintext_path": str(plaintext_output.resolve()),
            "plaintext_sha256": plaintext_sha256,
            "frida_version": version,
        }
    finally:
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        if state.get("remote_plaintext"):
            adb.su(f"rm -f {AdbClient.quote(state['remote_plaintext'])}", check=False)
        adb.su("pkill -f ohealth-frida-server", check=False)
        adb.run("forward", "--remove", "tcp:27042", check=False)
