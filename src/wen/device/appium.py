from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from wen.models import DeviceInfo

from .adb import AdbDevice
from .base import DeviceBackend, DeviceError

logger = logging.getLogger(__name__)


class AppiumDevice(DeviceBackend):
    name = "appium"
    # Douyin's home feed continuously emits accessibility updates.  The
    # UiAutomator2 default (10 seconds) makes every page-source/lookup call
    # wait unnecessarily long; workflow-level polling provides the readiness
    # check instead.
    UI_IDLE_TIMEOUT_MS = 300

    def __init__(
        self,
        server_url: str,
        serial: str | None,
        package: str,
        activity: str,
        adb_path: str = "adb",
    ) -> None:
        try:
            from appium import webdriver
            from appium.options.android import UiAutomator2Options
        except ImportError as exc:
            raise DeviceError(
                "未安装 Appium Python 客户端，请运行：uv sync --extra appium"
            ) from exc
        self._webdriver = webdriver
        self._options_cls = UiAutomator2Options
        self.server_url = server_url
        self.serial = serial
        self.package = package
        self.activity = activity
        self._adb_fallback = AdbDevice(serial=serial, adb_path=adb_path)
        self.driver: Any | None = None

    def _ensure_session(self) -> Any:
        if self.driver is not None:
            try:
                _ = self.driver.current_package
                return self.driver
            except Exception:  # noqa: BLE001 - stale Appium sessions expose vendor-specific errors
                self.driver = None
        options = self._options_cls()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.app_package = self.package
        options.app_activity = self.activity
        options.no_reset = True
        options.new_command_timeout = 120
        if self.serial:
            options.udid = self.serial
        try:
            self.driver = self._webdriver.Remote(self.server_url, options=options)
        except Exception as exc:
            raise DeviceError(f"无法连接 Appium 服务 {self.server_url}：{exc}") from exc
        try:
            # ``waitForIdleTimeout`` is a UiAutomator2 setting rather than a
            # session capability.  Keep it short and let the collector's
            # explicit predicates decide when a page is ready.
            self.driver.update_settings(
                {"waitForIdleTimeout": self.UI_IDLE_TIMEOUT_MS}
            )
        except Exception as exc:  # noqa: BLE001 - support older Appium server variants
            # Older Appium servers may not expose update_settings; retain the
            # normal session and let individual commands use their defaults.
            logger.debug("Appium update_settings is unavailable: %s", exc)
        return self.driver

    def info(self) -> DeviceInfo:
        driver = self._ensure_session()
        caps = driver.capabilities or {}
        size = driver.get_window_size()
        model = str(caps.get("deviceModel") or caps.get("deviceName") or "Android")
        serial = str(caps.get("udid") or caps.get("deviceUDID") or self.serial or "unknown")
        if serial != "unknown":
            # 让后续 ADB 取证始终命中与 Appium 相同的设备，避免多设备时误采集。
            self.serial = serial
            self._adb_fallback.serial = serial
        return DeviceInfo(
            serial=serial,
            state="device",
            model=model,
            android_version=str(caps.get("platformVersion") or "unknown"),
            is_emulator=bool(caps.get("isEmulator")) or "sdk" in model.lower(),
            screen_width=int(size.get("width", 0)),
            screen_height=int(size.get("height", 0)),
            backend=self.name,
        )

    def health_check(self) -> DeviceInfo:
        return self.info()

    def start_app(self, package: str, activity: str | None = None) -> None:
        driver = self._ensure_session()
        # 先终止再激活，避免 Android 恢复上一次停留的店铺详情页。no_reset
        # 仍会保留抖音登录态，但每次任务都从可预测的首页开始。部分 Appium
        # 版本的 start_activity 在 HyperOS 上会长时间阻塞，因此不作为首选。
        if package == self.package:
            try:
                driver.terminate_app(package)
            except Exception:  # noqa: BLE001 - a dead app is already in the desired state
                driver.activate_app(package)
                self._wait_until_ready(driver)
                return
            driver.activate_app(package)
            self._wait_until_ready(driver)
            return
        driver.start_activity(package, activity or "")

    @staticmethod
    def _wait_until_ready(driver: Any, timeout: float = 8.0) -> None:
        """Wait for Douyin's splash activity to hand off to its main UI.

        HyperOS can keep the process on a black splash screen for several
        seconds after ``activate_app``. Capturing or tapping during that
        interval leaves a stale UI hierarchy from the previous route, which
        is especially dangerous because a local shop-search label may then
        be mistaken for the global search entry.
        """
        deadline = time.monotonic() + timeout
        saw_non_splash = False
        while time.monotonic() < deadline:
            try:
                activity = str(driver.current_activity or "")
                if activity and "splash" not in activity.lower():
                    saw_non_splash = True
                    break
            except Exception:  # noqa: BLE001, S110 - activity is vendor/version dependent
                pass
            time.sleep(0.35)
        # Even builds that keep the splash activity name eventually expose a
        # usable hierarchy; leave a small settling period for that handoff.
        time.sleep(0.6 if saw_non_splash else 1.0)

    def stop_app(self, package: str) -> None:
        driver = self._ensure_session()
        driver.terminate_app(package)

    def open_uri(self, uri: str, package: str | None = None) -> None:
        # ADB's activity manager returns as soon as Android completes the route
        # transition.  Appium's deep-link wrapper adds a UIA2 round-trip and can
        # wait for Douyin's continuously changing accessibility tree to idle.
        self._adb_fallback.open_uri(uri, package or self.package)

    def screenshot(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 抖音/Android 15 在 UIA2 screenshot 端点上偶发使 instrumentation 崩溃。
        # ADB screencap 不依赖该端点，作为默认取证通道更稳定。
        try:
            self._adb_fallback.screenshot(destination)
        except Exception:  # noqa: BLE001 - ADB screenshot is a safe fallback for UIA2 crashes
            destination.write_bytes(self._ensure_session().get_screenshot_as_png())
        return destination

    def dump_ui(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Appium's page source follows the current activity.  On HyperOS 3,
        # ``uiautomator dump`` can lag one route behind while a canvas page is
        # animating (for example, it may still return the previous shop page
        # after the global search result is already visible).  Prefer the
        # session source and keep ADB as a compatibility fallback.
        try:
            xml = self._ensure_session().page_source
            if xml and xml.lstrip().startswith("<"):
                destination.write_text(xml, encoding="utf-8")
                return destination
        except Exception:  # noqa: BLE001, S110 - fall back to ADB UI dump
            pass
        self._adb_fallback.dump_ui(destination)
        return destination

    def tap(self, x: int, y: int) -> None:
        try:
            self._adb_fallback.tap(x, y)
        except Exception:  # noqa: BLE001 - fall back to Appium W3C tap
            self._ensure_session().tap([(x, y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        try:
            self._adb_fallback.swipe(x1, y1, x2, y2, duration_ms)
            return
        except Exception:  # noqa: BLE001 - fall back to Appium gesture
            driver = self._ensure_session()
        try:
            driver.swipe(x1, y1, x2, y2, duration_ms)
        except Exception:  # noqa: BLE001 - fallback to the W3C mobile swipe command
            driver.execute_script(
                "mobile: swipe",
                {"startX": x1, "startY": y1, "endX": x2, "endY": y2, "duration": duration_ms},
            )

    def scroll(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        *,
        direction: str = "down",
        percent: float = 0.4,
        speed: int = 1200,
    ) -> bool | None:
        """Dispatch a bounded coordinate swipe through ADB.

        UiAutomator2's ``scrollGesture`` is not a plain swipe: after the
        gesture it pauses and waits for an accessibility ``scrollFinished``
        event so it can return ``canScrollMore``. Douyin's custom-drawn list
        does not emit that event reliably, while the collector already checks
        the post-scroll card layout itself. ADB therefore avoids a redundant
        wait without changing the geometry or readiness checks.
        """
        return super().scroll(
            left,
            top,
            width,
            height,
            direction=direction,
            percent=percent,
            speed=speed,
        )

    def type_text(self, text: str) -> None:
        driver = self._ensure_session()
        # 抖音全局搜索页打开后会自动聚焦输入框。直接向当前焦点
        # 输入 Unicode，避免 find_elements -> click -> clear 的三次 UIA2 调用。
        try:
            driver.execute_script("mobile: type", {"text": text})
            return
        except Exception as first_error:  # noqa: BLE001 - retry after a stable coordinate tap
            # 极少数情况下搜索页动画尚未完成，焦点没有落入输入框。
            # 搜索框位置稳定；失败时才读屏幕尺寸，不增加正常路径开销。
            try:
                info = self.info()
                width = info.screen_width or 1080
                height = info.screen_height or 2400
                self.tap(width // 2, min(int(height * 0.08), 280))
                driver.execute_script("mobile: type", {"text": text})
                return
            except Exception as retry_error:  # noqa: BLE001 - expose both attempts as one device error
                raise DeviceError(
                    "抖音搜索框直接输入失败；当前页面可能尚未进入全局搜索："
                    f"{retry_error}"
                ) from first_error

    def press_enter(self) -> None:
        try:
            # ADB keyevent 不等待 UIA2 空闲，并且抖音会在 Enter 后自动
            # 关闭键盘。后续的页面 predicate 负责确认搜索结果已就绪。
            self._adb_fallback.press_enter()
        except Exception:  # noqa: BLE001 - retain Appium compatibility when ADB is unavailable
            self._ensure_session().press_keycode(66)

    def back(self) -> None:
        self._ensure_session().back()

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001 - device may go offline during Appium cleanup
                self.driver = None
            else:
                self.driver = None

    def tap_text(self, text: str) -> bool:
        # On recent Douyin/HyperOS builds, UiAutomator2 can expose a valid
        # element but block indefinitely while dispatching element.click().
        # Read the already-live Appium hierarchy, resolve bounds locally, and
        # dispatch a coordinate tap instead. This keeps the semantic lookup
        # while avoiding the hanging element-click command and a second ADB
        # UI dump (which can contend with the UiAutomator2 instrumentation).
        try:
            if self._tap_text_from_page_source(text, exact=False):
                return True
        except Exception:  # noqa: BLE001, S110 - fall back to the ADB hierarchy
            pass
        try:
            return self._adb_fallback.tap_text(text)
        except Exception:  # noqa: BLE001 - both semantic routes may be unavailable
            return False

    def tap_text_exact(self, text: str) -> bool:
        try:
            if self._tap_text_from_page_source(text, exact=True):
                return True
        except Exception:  # noqa: BLE001, S110 - fall back to the ADB hierarchy
            pass
        try:
            return self._adb_fallback.tap_text_exact(text)
        except Exception:  # noqa: BLE001 - both semantic routes may be unavailable
            return False

    def _tap_text_from_page_source(self, text: str, *, exact: bool) -> bool:
        from wen.extract.ui_xml import find_exact_text_bounds, find_text_bounds

        xml = self._ensure_session().page_source
        if not xml or not xml.lstrip().startswith("<"):
            return False
        bounds = (find_exact_text_bounds if exact else find_text_bounds)(xml, text)
        if not bounds:
            return False
        left, top, right, bottom = bounds
        self.tap((left + right) // 2, (top + bottom) // 2)
        return True

    def _tap_text_appium(self, text: str) -> bool:
        driver = self._ensure_session()
        from appium.webdriver.common.appiumby import AppiumBy

        locators = [
            # 抖音首页的搜索入口通常是 Button 的 content-desc，而不是 text 属性。
            (AppiumBy.ACCESSIBILITY_ID, text),
            (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{text}")'),
            (AppiumBy.XPATH, f'//*[contains(@text, "{text}") or contains(@content-desc, "{text}")]'),
        ]
        for by, value in locators:
            try:
                driver.find_element(by, value).click()
                return True
            except Exception:  # noqa: BLE001, S112 - try the next locator strategy
                continue
        return False

    def _tap_text_appium_exact(self, text: str) -> bool:
        driver = self._ensure_session()
        from appium.webdriver.common.appiumby import AppiumBy

        locators = [
            (AppiumBy.ACCESSIBILITY_ID, text),
            (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{text}")'),
            (AppiumBy.XPATH, f'//*[@text="{text}" or @content-desc="{text}"]'),
        ]
        for by, value in locators:
            try:
                driver.find_element(by, value).click()
                return True
            except Exception:  # noqa: BLE001, S112 - try the next locator strategy
                continue
        return False
