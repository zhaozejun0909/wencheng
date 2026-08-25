from __future__ import annotations

import base64
import copy
import xml.etree.ElementTree as ET
from pathlib import Path

from wen.device.base import DeviceBackend
from wen.models import DeviceInfo
from wen.platforms.douyin import DouyinExtractor

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FixtureDevice(DeviceBackend):
    """Offline device double used by tests; never exposed as a product backend."""

    name = "appium"
    is_live = False

    def __init__(self, fixture_xml: Path | None = None) -> None:
        self.fixture_xml = fixture_xml or Path("fixtures/douyin_shop.xml")
        self.started = False
        self._page_index = 0
        self._pages = self._build_catalog_pages()

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            serial="fixture-device",
            state="device",
            model="Fixture Android",
            android_version="15",
            is_emulator=True,
            screen_width=1080,
            screen_height=2400,
            backend=self.name,
        )

    def health_check(self) -> DeviceInfo:
        return self.info()

    def start_app(self, package: str, activity: str | None = None) -> None:
        self.started = True
        self._page_index = 0

    def stop_app(self, package: str) -> None:
        self.started = False

    def screenshot(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_PNG)
        return destination

    def dump_ui(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        page = self._pages[min(self._page_index, len(self._pages) - 1)]
        destination.write_text(page, encoding="utf-8")
        return destination

    def tap(self, x: int, y: int) -> None:
        return None

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        if self._pages:
            self._page_index = min(self._page_index + 1, len(self._pages) - 1)

    def _build_catalog_pages(self) -> list[str]:
        source = self.fixture_xml.read_text(encoding="utf-8")
        try:
            root = ET.fromstring(source)
            container = root.find("./node")
            if container is None:
                return [source]
            children = list(container)
            seed_store, seed_products = DouyinExtractor().extract(source, "鸭鸭童装旗舰店")
            if not seed_products:
                return [source]
            total = seed_store.product_count or len(seed_products)
            catalog = []
            for index in range(total):
                seed = seed_products[index % len(seed_products)]
                title = seed.title if index < len(seed_products) else f"鸭鸭童装测试商品{index + 1:02d} {seed.title[:16]}"
                catalog.append((title, seed.price or 99 + index, seed.displayed_sales_raw or f"{100 + index * 37}"))
            header = [copy.deepcopy(child) for child in children[:6]]
            pages: list[str] = []
            for page_start in range(0, len(catalog), 3):
                page_root = copy.deepcopy(root)
                page_container = page_root.find("./node")
                if page_container is None:
                    continue
                page_container.clear()
                for child in header:
                    page_container.append(copy.deepcopy(child))
                for slot, (title, price, sales) in enumerate(catalog[page_start : page_start + 3]):
                    top = 650 + slot * 250
                    page_container.extend(
                        [
                            ET.Element("node", {"text": title, "class": "android.widget.TextView", "bounds": f"[60,{top}][800,{top + 80}]"}),
                            ET.Element("node", {"text": f"¥{price:g}", "class": "android.widget.TextView", "bounds": f"[60,{top + 90}][260,{top + 150}]"}),
                            ET.Element("node", {"text": f"已售 {sales}", "class": "android.widget.TextView", "bounds": f"[300,{top + 90}][560,{top + 150}]"}),
                        ]
                    )
                pages.append(ET.tostring(page_root, encoding="unicode"))
            return pages or [source]
        except ET.ParseError:
            return [source]

    def type_text(self, text: str) -> None:
        return None

    def back(self) -> None:
        return None
