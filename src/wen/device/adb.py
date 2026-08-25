from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

from wen.models import DeviceInfo

from .base import DeviceBackend, DeviceError


class AdbDevice(DeviceBackend):
    name = "adb"

    def __init__(self, serial: str | None = None, adb_path: str = "adb") -> None:
        self.serial = serial
        self.adb_path = adb_path

    def _base(self) -> list[str]:
        command = [self.adb_path]
        if self.serial:
            command += ["-s", self.serial]
        return command

    def _run(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        command = self._base() + list(args)
        try:
            completed = subprocess.run(
                command,
                check=check,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise DeviceError(f"找不到 adb：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(f"adb 命令超时：{' '.join(command)}") from exc
        if completed.returncode != 0 and check:
            detail = (completed.stderr or completed.stdout).strip()
            raise DeviceError(f"adb 命令失败：{' '.join(command)}\n{detail}")
        return completed.stdout

    def _run_bytes(self, *args: str, timeout: float = 30) -> bytes:
        command = self._base() + list(args)
        try:
            completed = subprocess.run(command, check=True, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise DeviceError(f"找不到 adb：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(f"adb 命令超时：{' '.join(command)}") from exc
        return completed.stdout

    def _shell(self, *args: str, timeout: float = 30) -> str:
        return self._run("shell", *args, timeout=timeout)

    @staticmethod
    def _prop(props: str, key: str) -> str | None:
        match = re.search(rf"^\[{re.escape(key)}\]: \[(.*?)\]$", props, re.MULTILINE)
        return match.group(1) if match else None

    def _resolve_serial(self) -> str:
        if self.serial:
            return self.serial
        output = subprocess.run(
            [self.adb_path, "devices"], capture_output=True, text=True, check=False
        ).stdout
        devices = [
            line.split("\t", 1)[0]
            for line in output.splitlines()[1:]
            if "\tdevice" in line
        ]
        if not devices:
            raise DeviceError("没有发现已授权的 Android 设备。请检查 USB 调试和 adb devices。")
        self.serial = devices[0]
        return self.serial

    def info(self) -> DeviceInfo:
        serial = self._resolve_serial()
        raw_devices = subprocess.run(
            [self.adb_path, "devices"], capture_output=True, text=True, check=False
        ).stdout
        state = "unknown"
        for line in raw_devices.splitlines():
            if line.startswith(serial + "\t"):
                state = line.split("\t", 1)[1].split()[0]
                break
        props = self._shell("getprop") if state == "device" else ""
        screen = self._shell("wm", "size") if state == "device" else ""
        match = re.search(r"Physical size: (\d+)x(\d+)", screen)
        return DeviceInfo(
            serial=serial,
            state=state,
            model=self._prop(props, "ro.product.model"),
            android_version=self._prop(props, "ro.build.version.release"),
            is_emulator=self._prop(props, "ro.kernel.qemu") == "1"
            or bool(self._prop(props, "ro.product.model") and "sdk_gphone" in self._prop(props, "ro.product.model")),
            screen_width=int(match.group(1)) if match else None,
            screen_height=int(match.group(2)) if match else None,
            backend=self.name,
        )

    def health_check(self) -> DeviceInfo:
        device = self.info()
        if device.state != "device":
            raise DeviceError(f"设备状态不是 device：{device.state}（序列号 {device.serial}）")
        return device

    def start_app(self, package: str, activity: str | None = None) -> None:
        self.health_check()
        component = f"{package}/{activity}" if activity else package
        self._run("shell", "am", "start", "-n", component)

    def stop_app(self, package: str) -> None:
        # Emergency stop must not inherit the general 30-second ADB timeout.
        # If USB is unhealthy, the caller should continue terminating the
        # worker instead of blocking the stop endpoint for half a minute.
        self._run("shell", "am", "force-stop", package, timeout=3, check=False)

    def open_uri(self, uri: str, package: str | None = None) -> None:
        # adb itself receives an argv array, but `adb shell` serializes the
        # remaining arguments and the device-side shell parses them again.
        # Quote the URI for that remote shell so '&promotion_id=...' cannot be
        # split into a second command (observed as exit code 127 on the phone).
        command = [
            "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
            "-d", shlex.quote(uri),
        ]
        if package:
            command.append(package)
        self._run(*command, timeout=12)

    def screenshot(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._run_bytes("exec-out", "screencap", "-p"))
        return destination

    def dump_ui(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        remote = "/sdcard/wen-window.xml"
        self._run("shell", "uiautomator", "dump", remote, check=False)
        xml = self._run("shell", "cat", remote)
        if xml.startswith("UI hierchary dumped to"):
            xml = xml.split("\n", 1)[-1]
        if not xml.lstrip().startswith("<"):
            raise DeviceError("ADB UI 层级为空或无效，页面可能正在切换。")
        destination.write_text(xml, encoding="utf-8")
        return destination

    def tap(self, x: int, y: int) -> None:
        self._shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        self._shell(
            "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)
        )

    def type_text(self, text: str) -> None:
        if any(ord(char) > 127 for char in text):
            raise DeviceError("ADB input text 不支持中文；请用 Appium 或通过 scrcpy 手动输入。")
        self._shell("input", "text", text.replace(" ", "%s"))

    def press_enter(self) -> None:
        self._shell("input", "keyevent", "66")

    def back(self) -> None:
        self._shell("input", "keyevent", "4")

    def tap_text(self, text: str) -> bool:
        temp = Path("/tmp/wen-adb-ui.xml")
        self.dump_ui(temp)
        from wen.extract.ui_xml import find_text_bounds

        bounds = find_text_bounds(temp.read_text(encoding="utf-8"), text)
        if not bounds:
            return False
        left, top, right, bottom = bounds
        self.tap((left + right) // 2, (top + bottom) // 2)
        return True

    def tap_text_exact(self, text: str) -> bool:
        temp = Path("/tmp/wen-adb-ui.xml")
        self.dump_ui(temp)
        from wen.extract.ui_xml import find_exact_text_bounds

        bounds = find_exact_text_bounds(temp.read_text(encoding="utf-8"), text)
        if not bounds:
            return False
        left, top, right, bottom = bounds
        self.tap((left + right) // 2, (top + bottom) // 2)
        return True

    def wait_for_device(self, timeout: float = 30) -> DeviceInfo:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                device = self.info()
                if device.state == "device":
                    return device
            except DeviceError:
                pass
            time.sleep(0.5)
        raise DeviceError("等待 Android 设备超时")
