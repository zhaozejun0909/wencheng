from __future__ import annotations

from types import SimpleNamespace

from wen.device.appium import AppiumDevice


class FakeDriver:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.scripts: list[tuple[str, dict[str, str]]] = []
        self.keycodes: list[int] = []

    def execute_script(self, name: str, payload: dict[str, str]) -> None:
        self.scripts.append((name, payload))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("input is not focused")

    def press_keycode(self, keycode: int) -> None:
        self.keycodes.append(keycode)


class FakeAdb:
    def __init__(self, fail_enter: bool = False) -> None:
        self.fail_enter = fail_enter
        self.enter_count = 0
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.uris: list[tuple[str, str | None]] = []

    def press_enter(self) -> None:
        self.enter_count += 1
        if self.fail_enter:
            raise RuntimeError("adb unavailable")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))

    def open_uri(self, uri: str, package: str | None = None) -> None:
        self.uris.append((uri, package))


def appium_device(driver: FakeDriver, adb: FakeAdb) -> AppiumDevice:
    device = object.__new__(AppiumDevice)
    device.driver = driver
    device._adb_fallback = adb
    device._ensure_session = lambda: driver
    return device


def test_type_text_uses_focused_unicode_input_without_element_lookup() -> None:
    driver = FakeDriver()
    device = appium_device(driver, FakeAdb())

    device.type_text("鸭鸭童装官方旗舰店")

    assert driver.scripts == [
        ("mobile: type", {"text": "鸭鸭童装官方旗舰店"})
    ]


def test_type_text_taps_fixed_search_box_only_after_direct_input_fails() -> None:
    driver = FakeDriver(failures=1)
    device = appium_device(driver, FakeAdb())
    taps: list[tuple[int, int]] = []
    device.info = lambda: SimpleNamespace(screen_width=1280, screen_height=2772)
    device.tap = lambda x, y: taps.append((x, y))

    device.type_text("鸭鸭")

    assert taps == [(640, 221)]
    assert driver.scripts == [
        ("mobile: type", {"text": "鸭鸭"}),
        ("mobile: type", {"text": "鸭鸭"}),
    ]


def test_press_enter_prefers_adb_and_falls_back_to_appium() -> None:
    driver = FakeDriver()
    adb = FakeAdb()
    device = appium_device(driver, adb)
    device.press_enter()
    assert adb.enter_count == 1
    assert driver.keycodes == []

    fallback_driver = FakeDriver()
    fallback_adb = FakeAdb(fail_enter=True)
    fallback_device = appium_device(fallback_driver, fallback_adb)
    fallback_device.press_enter()
    assert fallback_adb.enter_count == 1
    assert fallback_driver.keycodes == [66]


def test_scroll_uses_adb_swipe_without_uiautomator_scroll_wait() -> None:
    driver = FakeDriver()
    adb = FakeAdb()
    device = appium_device(driver, adb)

    result = device.scroll(0, 400, 1280, 2100, percent=0.6, speed=1200)

    assert result is None
    assert adb.swipes == [(640, 2476, 640, 1216, 1050)]
    assert driver.scripts == []


def test_open_uri_uses_adb_without_appium_ui_round_trip() -> None:
    driver = FakeDriver()
    adb = FakeAdb()
    device = appium_device(driver, adb)
    device.package = "com.ss.android.ugc.aweme"

    device.open_uri(
        "snssdk1128://ec_goods_detail?product_id=123&promotion_id=123"
    )

    assert adb.uris == [
        (
            "snssdk1128://ec_goods_detail?product_id=123&promotion_id=123",
            "com.ss.android.ugc.aweme",
        )
    ]
    assert driver.scripts == []
