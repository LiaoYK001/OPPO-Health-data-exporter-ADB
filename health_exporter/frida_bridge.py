from __future__ import annotations

import hashlib
import json
import lzma
import os
import re
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .adb import AdbClient
from .constants import PACKAGE
from .errors import ExporterError, RootRequiredError


ABI_NAMES = {
    "arm64-v8a": "arm64",
    "armeabi-v7a": "arm",
    "x86_64": "x86_64",
    "x86": "x86",
}
FRIDA_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*$")
GITHUB_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_RELEASE_METADATA_BYTES = 8 * 1024 * 1024
MAX_SERVER_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_SERVER_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


def _validate_github_url(url: str, *, asset: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ExporterError("Frida 下载地址必须是无凭据的 HTTPS URL。")
    host = (parsed.hostname or "").lower()
    if host not in GITHUB_DOWNLOAD_HOSTS:
        raise ExporterError(f"拒绝访问非 GitHub 下载主机：{host or 'unknown'}")
    if asset and host == "github.com" and not parsed.path.startswith(
        "/frida/frida/releases/download/"
    ):
        raise ExporterError("Frida 资源地址不属于 frida/frida 官方 Release。")


def _open_github_url(
    request: str | urllib.request.Request,
    *,
    timeout: int,
    asset: bool = False,
) -> Any:
    initial_url = request.full_url if isinstance(request, urllib.request.Request) else request
    _validate_github_url(initial_url, asset=asset)
    response = urllib.request.urlopen(request, timeout=timeout)  # nosec B310
    try:
        _validate_github_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def _read_limited(response: Any, maximum: int, label: str) -> bytes:
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > maximum:
                raise ExporterError(f"{label} 超过允许的 {maximum} 字节。")
        except ValueError as exc:
            raise ExporterError(f"{label} 返回了无效的 Content-Length。") from exc
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise ExporterError(f"{label} 超过允许的 {maximum} 字节。")
    return payload


def _decompress_limited(payload: bytes, maximum: int) -> bytes:
    decompressor = lzma.LZMADecompressor()
    try:
        result = decompressor.decompress(payload, max_length=maximum + 1)
    except lzma.LZMAError as exc:
        raise ExporterError("Frida Server 压缩包不是有效的 XZ 数据。") from exc
    if len(result) > maximum or not decompressor.eof:
        raise ExporterError(f"Frida Server 解压后超过允许的 {maximum} 字节。")
    return result


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
    if not FRIDA_VERSION_PATTERN.fullmatch(version):
        raise ExporterError(f"Frida 客户端版本格式无效：{version!r}")
    android_abi = adb.shell("getprop", "ro.product.cpu.abi")
    abi = ABI_NAMES.get(android_abi)
    if abi is None:
        raise ExporterError(f"Frida 暂不支持该模拟器 ABI：{android_abi}")
    asset_name = f"frida-server-{version}-android-{abi}.xz"
    tools_dir = private_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    compressed = tools_dir / asset_name
    server = tools_dir / asset_name.removesuffix(".xz")
    server_digest_path = server.with_suffix(server.suffix + ".sha256")
    if server.exists():
        if not server_digest_path.exists():
            raise ExporterError("本地 Frida Server 缺少 SHA-256 校验文件，拒绝继续使用。")
        expected_server = server_digest_path.read_text(encoding="ascii").strip()
        actual_server = hashlib.sha256(server.read_bytes()).hexdigest()
        if actual_server != expected_server:
            raise ExporterError("本地 Frida Server 哈希不匹配，拒绝继续使用。")
        for path in (tools_dir, server, server_digest_path):
            try:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        return server, version
    release_url = f"https://api.github.com/repos/frida/frida/releases/tags/{version}"
    request = urllib.request.Request(release_url, headers={"User-Agent": "ohealth-exporter"})
    try:
        with _open_github_url(request, timeout=30) as response:
            release = json.loads(
                _read_limited(
                    response, MAX_RELEASE_METADATA_BYTES, "Frida Release 元数据"
                )
            )
    except Exception as exc:
        raise ExporterError(f"无法读取 Frida 官方发布信息：{exc}") from exc
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise ExporterError("Frida Release 元数据格式无效。")
    asset = next(
        (
            item
            for item in release["assets"]
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if asset is None:
        raise ExporterError(f"Frida 发布 {version} 中没有 {asset_name}")
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ExporterError("Frida 官方发布未提供 SHA-256，拒绝使用未校验二进制。")
    expected = digest.split(":", 1)[1].lower()
    download_url = str(asset.get("browser_download_url") or "")
    _validate_github_url(download_url, asset=True)
    try:
        with _open_github_url(download_url, timeout=120, asset=True) as response:
            payload = _read_limited(
                response, MAX_SERVER_DOWNLOAD_BYTES, "Frida Server"
            )
    except Exception as exc:
        raise ExporterError(f"下载 Frida Server 失败：{exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ExporterError(
            f"Frida Server SHA-256 不匹配：expected={expected}, actual={actual}"
        )
    compressed.write_bytes(payload)
    server_payload = _decompress_limited(payload, MAX_SERVER_UNCOMPRESSED_BYTES)
    server.write_bytes(server_payload)
    server_digest_path.write_text(
        hashlib.sha256(server_payload).hexdigest(), encoding="ascii"
    )
    compressed.unlink(missing_ok=True)
    for path in (tools_dir, server, server_digest_path):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    return server, version


def capture_database_key(
    adb: AdbClient,
    private_dir: Path,
    plaintext_output: Path,
    timeout: int = 45,
    process_name: str = PACKAGE,
) -> dict[str, Any]:
    """Capture the passphrase and export a temporary plaintext copy through SQLCipher."""
    if not adb.root_available():
        raise RootRequiredError("key-capture 需要短暂开启 MuMu Root 并重启。")
    pid_values = adb.shell("pidof", process_name, check=False).split()
    if len(pid_values) != 1 or not pid_values[0].isdigit():
        raise ExporterError(
            f"未找到唯一的 OPPO 健康进程 {process_name}。key-capture 不会自动启动 APP；"
            "请关闭 Root、启动 APP，再热开启 Root。"
        )
    app_pid = int(pid_values[0])
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
send({kind: 'ohealth-stage', value: 'loaded;java=' + String(Java.available)});
if (!Java.available) {
  send({kind: 'ohealth-db-export-error', value: 'Java VM is not available in target process'});
}
Java.performNow(function () {
  send({kind: 'ohealth-stage', value: 'java-ready'});
  const Encryptor = Java.use('com.oplus.aiunit.vision.ev4');
  const SQLiteDatabase = Java.use('net.zetetic.database.sqlcipher.SQLiteDatabase');
  send({kind: 'ohealth-stage', value: 'database-class-ready'});
  function exportDatabase(context) {
      const passphrase = Encryptor.$new().t0(context);
      send({kind: 'ohealth-db-key', value: String(passphrase)});
      const source = context.getDatabasePath('database.db').getAbsolutePath();
      const output = context.getDatabasePath('ohealth-plaintext-export.db').getAbsolutePath();
      try {
        const File = Java.use('java.io.File');
        const outputFile = File.$new(output);
        outputFile.delete();
        if (!outputFile.createNewFile()) {
          throw new Error('unable to create temporary database file');
        }
        const openDatabase = SQLiteDatabase.openDatabase.overload(
          'java.lang.String',
          'java.lang.String',
          'net.zetetic.database.sqlcipher.SQLiteDatabase$CursorFactory',
          'int',
          'net.zetetic.database.sqlcipher.SQLiteDatabaseHook'
        );
        const db = openDatabase.call(SQLiteDatabase, source, String(passphrase), null, 0, null);
        db.execSQL("ATTACH DATABASE '" + output + "' AS plaintext KEY ''");
        const exportCursor = db.query("SELECT sqlcipher_export('plaintext')");
        exportCursor.moveToFirst();
        exportCursor.close();
        db.execSQL('DETACH DATABASE plaintext');
        db.close();
        send({kind: 'ohealth-db-export', value: String(output)});
      } catch (error) {
        send({kind: 'ohealth-db-export-error', value: String(error.stack || error)});
      }
  }
  (function () {
    send({kind: 'ohealth-stage', value: 'context-lookup'});
    function openDatabase(application) {
      exportDatabase(application.getApplicationContext());
    }
    try {
      const ActivityThread = Java.use('android.app.ActivityThread');
      let application = ActivityThread.currentApplication();
      if (application !== null) {
        openDatabase(application);
        return;
      }
      const AppGlobals = Java.use('android.app.AppGlobals');
      application = AppGlobals.getInitialApplication();
      if (application !== null) {
        openDatabase(application);
        return;
      }
      let matched = false;
      Java.choose('com.heytap.health.main.MainActivity', {
        onMatch: function (activity) {
          if (!matched) {
            matched = true;
            openDatabase(activity);
          }
        },
        onComplete: function () {
          if (!matched) {
            send({kind: 'ohealth-db-export-error', value: 'no Application or MainActivity instance'});
          }
        }
      });
    } catch (error) {
      send({kind: 'ohealth-db-export-error', value: String(error.stack || error)});
    }
  })();
});
"""
    if int(version.split(".", 1)[0]) >= 17:
        agent_dir = private_dir / "tools" / "frida-agent"
        bridge_dir = agent_dir / "node_modules" / "frida-java-bridge"
        if not bridge_dir.exists():
            raise ExporterError(
                "Frida 17 需要 Java Bridge。请执行："
                f"npm install --prefix \"{agent_dir}\" frida-java-bridge"
            )
        entry = agent_dir / "ohealth-agent.js"
        entry.write_text(
            'import Java from "frida-java-bridge";\n' + script_source,
            encoding="utf-8",
        )
        diagnostics: list[str] = []
        compiler = frida.Compiler()
        compiler.on("diagnostics", lambda value: diagnostics.append(str(value)))
        try:
            script_source = compiler.build(str(entry), project_root=str(agent_dir))
        except Exception as exc:
            detail = "; ".join(diagnostics[-5:])
            raise ExporterError(f"编译 Frida Java Bridge 失败：{exc}; {detail}") from exc

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
                key_found.set()
                export_finished.set()
            elif payload.get("kind") == "ohealth-stage":
                state.setdefault("stages", []).append(str(payload["value"]))
        elif message.get("type") == "error":
            state["error"] = message.get("stack") or message.get("description")
            key_found.set()
            export_finished.set()

    session = None
    script = None
    try:
        manager = frida.get_device_manager()
        device = manager.add_remote_device("127.0.0.1:27042")
        try:
            session = device.attach(app_pid)
        except Exception as exc:
            raise ExporterError(
                f"无法附加到已确认的 OPPO 健康进程 {process_name} (PID {app_pid})；"
                "进程可能已退出，命令不会自动重启 APP。"
            ) from exc
        script = session.create_script(script_source)
        script.on("message", on_message)
        script.load()
        if not key_found.wait(timeout):
            raise ExporterError(
                "等待数据库初始化超时。阶段="
                f"{','.join(state.get('stages', [])) or 'none'}。"
                "保持 Root 开启，重新运行 key-capture；无需在风险提示中点击确认。"
            )
        if "key" not in state:
            raise ExporterError(
                "Frida 脚本失败："
                f"{state.get('export_error', state.get('error', 'unknown error'))}"
            )
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
            "process_name": process_name,
        }
    finally:
        if script is not None:
            try:
                script.unload()
            # Best-effort cleanup continues with detach and device-side removal.
            except Exception:  # nosec B110
                pass
        if session is not None:
            try:
                session.detach()
            # A dead target process can make detach fail after export completed.
            except Exception:  # nosec B110
                pass
        if state.get("remote_plaintext"):
            adb.su(f"rm -f {AdbClient.quote(state['remote_plaintext'])}", check=False)
        adb.su(
            "rm -f /data/user/0/com.heytap.health/cache/ohealth-plaintext-export.db "
            "/data/user/0/com.heytap.health/databases/ohealth-plaintext-export.db",
            check=False,
        )
        adb.su("pkill -f ohealth-frida-server", check=False)
        adb.run("forward", "--remove", "tcp:27042", check=False)
