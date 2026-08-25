from __future__ import annotations

import json
import logging
import re
import statistics
import time
import uuid
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote

from wen.config import Settings
from wen.device import (
    DeviceBackend,
    DeviceError,
    LoginRequired,
    RiskControlDetected,
    StoreSelectionRequired,
)
from wen.extract import NullOcrProvider, OcrDetection, OcrProvider, extract_texts, parse_ui_xml
from wen.extract.ui_xml import find_exact_text_bounds
from wen.images import (
    capture_favorite_product_images,
    capture_product_detail_image,
    capture_product_images,
)
from wen.models import (
    CollectionResult,
    FieldValue,
    JobStatus,
    PreciseQueryMode,
    ProductRecord,
    ProductSelection,
    ProductSelectionMode,
    ProductSortMode,
    StoreLocatorMode,
    StoreRecord,
)
from wen.platforms.douyin import DouyinExtractor, parse_count, parse_price
from wen.storage import DataStore

logger = logging.getLogger(__name__)

_LOGIN_MARKERS = ("登录后", "手机号登录", "验证码登录", "密码登录", "登录/注册")
_RISK_MARKERS = ("访问频繁", "请完成验证", "安全验证", "滑块验证", "验证码", "Application has no permissions")
_FAVORITE_INVALID_MARKERS = (
    "已失效", "商品失效", "已下架", "商品已下架", "已删除", "无法购买", "暂不可用",
)
_FAVORITE_END_MARKERS = ("你可能还会喜欢",)
_FAVORITE_SALES_RE = re.compile(
    r"(?:已售|销量|销售|售出)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万千百亿kKmMwW]?\+?)"
)


class Collector:
    def __init__(
        self,
        settings: Settings,
        device: DeviceBackend,
        store: DataStore,
        ocr: OcrProvider | None = None,
    ) -> None:
        self.settings = settings
        self.device = device
        self.store = store
        self.ocr = ocr or NullOcrProvider()
        self.extractor = DouyinExtractor()
        self._auto_ocr_provider: OcrProvider | None = None
        self._auto_ocr_attempted = False
        self._auto_ocr_error: str | None = None

    def run(
        self,
        keyword: str,
        max_products: int | None = None,
        job_id: str | None = None,
        store_name: str | None = None,
        product_titles: list[str] | None = None,
        product_selections: list[ProductSelection] | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        selection_mode: ProductSelectionMode | str = ProductSelectionMode.RANGE,
        sort_mode: ProductSortMode | str = ProductSortMode.COMPREHENSIVE,
        query_group_id: str | None = None,
        query_group_name: str | None = None,
        query_run_id: str | None = None,
        precise_query_mode: PreciseQueryMode | str = PreciseQueryMode.STORE,
        store_locator_mode: StoreLocatorMode | str = StoreLocatorMode.NAME,
        sec_shop_id: str | None = None,
        product_ids: list[str] | None = None,
    ) -> CollectionResult:
        job_id = job_id or uuid.uuid4().hex[:12]
        selection_mode = ProductSelectionMode(selection_mode)
        sort_mode = ProductSortMode(sort_mode)
        precise_query_mode = PreciseQueryMode(precise_query_mode)
        if selection_mode == ProductSelectionMode.PRECISE:
            # 精准查询现在只有商品 ID 直达这一条路径；不再回退到店铺列表匹配。
            precise_query_mode = PreciseQueryMode.PRODUCT_IDS
        store_locator_mode = StoreLocatorMode(store_locator_mode)
        requested_product_ids = self._normalize_product_ids(product_ids or [])
        product_id_mode = selection_mode == ProductSelectionMode.PRECISE
        requested_selections = self._coerce_product_selections(
            product_selections, product_titles
        )
        favorites_mode = selection_mode == ProductSelectionMode.FAVORITES
        direct_store_mode = (
            not favorites_mode
            and not product_id_mode
            and store_locator_mode == StoreLocatorMode.SEC_SHOP_ID
        )
        requested_sec_shop_id = (
            (sec_shop_id or "").strip() if direct_store_mode else None
        ) or None
        requested_store_name = (
            None
            if favorites_mode or product_id_mode or direct_store_mode
            else (store_name or keyword).strip()
        )
        if favorites_mode:
            # Keep a stable scope keyword for legacy clients, but do not bind the result to a store.
            keyword = "我的收藏"
        elif product_id_mode:
            keyword = "商品 ID 查询"
        elif direct_store_mode:
            keyword = requested_sec_shop_id or "sec_shop_id"
        result = CollectionResult(
            job_id=job_id,
            query_run_id=query_run_id,
            status=JobStatus.RUNNING,
            backend=self.device.name,
            keyword=keyword,
            requested_store_name=requested_store_name,
            requested_sec_shop_id=requested_sec_shop_id,
            precise_query_mode=precise_query_mode,
            query_group_id=query_group_id,
            query_group_name=query_group_name,
            selection_mode=selection_mode,
            sort_mode=sort_mode,
            state="device_check",
        )
        self.store.create_job(result)
        evidence_dir = self.settings.evidence_dir / f"{job_id}_{self._safe_name(keyword)}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        result.evidence_dir = str(evidence_dir)
        verified_store: StoreRecord | None = None
        try:
            self.device.health_check()
            if product_id_mode:
                if not requested_product_ids:
                    raise ValueError("根据商品 ID 精准查询至少需要填写一个有效商品 ID。")
                self._set_state(result, "product_detail")
                result.products = self._collect_product_ids(
                    requested_product_ids, evidence_dir, result
                )
                if not result.products:
                    raise RuntimeError(
                        "所有商品 ID 均未能读取到有效详情："
                        + "、".join(result.failed_product_ids)
                    )
                if result.failed_product_ids:
                    result.warnings.append(
                        f"有 {len(result.failed_product_ids)} 个商品 ID 无法打开或已失效："
                        + "、".join(result.failed_product_ids)
                    )
                result.status = JobStatus.SUCCEEDED
                self._set_state(result, "completed")
                return result
            self._set_state(result, "app_start")
            if direct_store_mode:
                self.device.open_uri(
                    self._store_deep_link(requested_sec_shop_id or ""),
                    self.settings.douyin_package,
                )
            else:
                self.device.start_app(self.settings.douyin_package, self.settings.douyin_activity)

            self._set_state(result, "page_capture")
            if self.device.is_live:
                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    "01_start",
                    (
                        (lambda _xml, values: self._is_store_page(values))
                        if direct_store_mode
                        else (
                            lambda _xml, values: self._is_home_page(values)
                            or "我" in values
                            or "我的" in values
                        )
                    ),
                    timeout=10.0 if direct_store_mode else 8.0,
                    min_settle=0.25,
                )
                ui_path = evidence_dir / "01_start.xml"
                ui_path.write_text(xml, encoding="utf-8")
            else:
                ui_path = self.device.dump_ui(evidence_dir / "01_start.xml")
                xml = ui_path.read_text(encoding="utf-8")
                texts = extract_texts(xml)
                # Fixture/offline devices do not need a screenshot.  Keep a
                # sentinel path so the later image/OCR helpers can safely
                # no-op without reintroducing a capture in the XML-only
                # startup path.
                screenshot = evidence_dir / "__not_captured__.png"
            # The home-page XML already exposes the global search entry and
            # login/overlay markers.  Do not block startup with a screenshot
            # or OCR pass here; screenshots/OCR remain available in the later
            # canvas product-list stages where XML is insufficient.
            ocr_texts: list[str] = []
            (evidence_dir / "01_texts.json").write_text(
                json.dumps({"ui": texts, "ocr": ocr_texts}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not direct_store_mode and self._dismiss_common_overlay(texts):
                if self.device.is_live:
                    xml, texts = self._wait_for_ui(
                        evidence_dir,
                        "01_after_overlay",
                        lambda _xml, values: not any(
                            marker in " ".join(values)
                            for marker in ("未成年人模式", "青少年模式")
                        ),
                        timeout=4.0,
                        min_settle=0.2,
                    )
                    ui_path = evidence_dir / "01_after_overlay.xml"
                    ui_path.write_text(xml, encoding="utf-8")
                else:
                    ui_path = self.device.dump_ui(evidence_dir / "01_after_overlay.xml")
                    xml = ui_path.read_text(encoding="utf-8")
                    texts = extract_texts(xml)
                ocr_texts = []
            self._guard_page(texts + ocr_texts)

            if self._login_required(texts + ocr_texts):
                raise LoginRequired("抖音当前未登录，请在真机或 scrcpy 中完成登录后重新运行任务。")

            self._set_state(
                result,
                "favorites_open"
                if favorites_mode
                else "store_direct"
                if direct_store_mode
                else "search_or_extract",
            )
            favorite_ocr_detections: list[OcrDetection] = []
            favorite_collection_complete = False
            if direct_store_mode and self.device.is_live:
                # The deep link has already landed on the store page.  Do not
                # visit home, global search or the store-result list.
                (evidence_dir / "04_store.xml").write_text(xml, encoding="utf-8")
                screenshot = self.device.screenshot(evidence_dir / "04_store.png")
                ocr_texts = self._plain_ocr_texts_if_needed(screenshot)
                self._guard_page(texts + ocr_texts)
                if not self._is_store_page(texts + ocr_texts):
                    raise RuntimeError(
                        f"已按 sec_shop_id={requested_sec_shop_id} 发起直达，但未进入店铺页；"
                        "请检查 sec_shop_id 是否完整、店铺是否有效。"
                    )
                verified_store, _unused_products = self.extractor.extract(
                    xml, requested_sec_shop_id or keyword, ocr_texts
                )
                verified_store.douyin_id = requested_sec_shop_id
                xml = self._prepare_store_product_view(xml, evidence_dir)
                self._prepare_product_list(sort_mode, xml, evidence_dir)
                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    "05_products",
                    lambda current_xml, values: self._xml_has_selected_label(current_xml, "商品")
                    and bool(DouyinExtractor._title_nodes(parse_ui_xml(current_xml))),
                    timeout=10.0,
                    min_settle=0.25,
                )
                (evidence_dir / "05_products.xml").write_text(xml, encoding="utf-8")
                screenshot = self.device.screenshot(evidence_dir / "05_products.png")
                ocr_texts = self._plain_ocr_texts_if_needed(screenshot)
                self._guard_page(texts + ocr_texts)
            elif favorites_mode and self.device.is_live:
                self._open_favorites(evidence_dir)
                # _open_favorites leaves the app on the dedicated 商品收藏列表页;
                # the preview card on the personal page must never be parsed as a list.
                ui_path = self.device.dump_ui(evidence_dir / "05_favorites_products.xml")
                xml = ui_path.read_text(encoding="utf-8")
                screenshot = self.device.screenshot(evidence_dir / "05_favorites_products.png")
                texts = extract_texts(xml)
                ocr_texts = self._plain_ocr_texts_if_needed(screenshot)
                # The 商品收藏 title is drawn on the canvas and is absent from the UI XML
                # on some app versions.  Run the same local, screenshot-only OCR used for
                # product fields before validating the page, then merge its text labels.
                favorite_ocr_detections = self._product_ocr_detections(
                    screenshot, evidence_dir, "05_favorites_products", xml=xml
                )
                if favorite_ocr_detections:
                    ocr_texts = list(
                        dict.fromkeys(
                            [*ocr_texts, *(item.text for item in favorite_ocr_detections)]
                        )
                    )
                self._guard_page(texts + ocr_texts)
                if not self._is_favorite_product_list(texts + ocr_texts):
                    raise RuntimeError(
                        "已点击“查看全部”，但当前页面仍不是商品收藏列表；"
                        "已保留 05_favorites_products.xml/png 及可用的 OCR 现场证据。"
                    )
                # “你可能还会喜欢” marks the exact end of the user's collection;
                # cards below it are recommendations and must not be paged/parsed.
                favorite_collection_complete = self._favorite_collection_end_seen(
                    xml, favorite_ocr_detections
                )
            elif self.device.is_live:
                # ``01_start`` has already been settled against the home-page
                # markers above.  Re-dumping the entire hierarchy here is
                # redundant on Douyin's continuously animated home feed and
                # can hit UiAutomator2's idle timeout.  Keep the recovery path
                # for a restored store/search route, but skip it when the
                # latest verified hierarchy is already the home page.
                if not self._is_home_page(texts):
                    self._prepare_global_search(evidence_dir)
                self._search(keyword, evidence_dir, home_xml=xml)
                # 提交后第一份搜索结果 XML 已经包含全部 Tab。只等待
                # 目标“店铺”按钮出现，然后复用同一份 XML 的坐标直接点击；
                # 不再先等综合内容、截图，再让 tap_text 重读一次页面。
                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    "02_search",
                    lambda current_xml, _values: bool(
                        find_exact_text_bounds(current_xml, "店铺")
                    ),
                    timeout=8.0,
                    min_settle=0.0,
                )
                ui_path = evidence_dir / "02_search.xml"
                ui_path.write_text(xml, encoding="utf-8")
                self._guard_page(texts)
                store_tab_bounds = find_exact_text_bounds(xml, "店铺")
                if not store_tab_bounds:
                    # 正常路径不需要这张图；只在失败时留现场。
                    self.device.screenshot(evidence_dir / "02_search.png")
                    raise RuntimeError(
                        "搜索已提交，但 8 秒内未找到搜索结果的“店铺”标签；"
                        "可能仍在加载或当前抖音页面状态异常，已保留 02_search.xml/png。"
                    )
                left, top, right, bottom = store_tab_bounds
                self.device.tap((left + right) // 2, (top + bottom) // 2)
                # 店铺卡是抖音 Canvas 自绘内容，很多版本永远不会出现在
                # UI XML。这里必须轮询截图 OCR；只轮询 XML 会等满超时，
                # 造成“已经看到店铺卡但后台还要停很久”的假慢。
                store_screenshot = evidence_dir / "03_store_search.png"
                xml, texts, store_ocr_detections = self._wait_for_store_result(
                    evidence_dir,
                    requested_store_name,
                    timeout=25.0,
                    screenshot_path=store_screenshot,
                )
                ui_path = evidence_dir / "03_store_search.xml"
                ui_path.write_text(xml, encoding="utf-8")
                # If the result was recognized from a probe, keep that exact
                # frame paired with its OCR boxes; otherwise capture the final
                # XML state once the bounded wait has expired.
                screenshot = store_screenshot
                if not screenshot.exists():
                    self.device.screenshot(screenshot)
                ocr_texts = [item.text for item in store_ocr_detections]
                if not ocr_texts and self.ocr.name != "none":
                    ocr_texts = self.ocr.recognize(screenshot)
                self._guard_page(texts + ocr_texts)
                candidates = self._detect_store_candidates(
                    xml, screenshot, evidence_dir, ocr_detections=store_ocr_detections
                )
                result.store_candidates = [name for name, _bounds in candidates]
                (evidence_dir / "03_store_candidates.json").write_text(
                    json.dumps(
                        {
                            "requested_store_name": requested_store_name,
                            "candidates": result.store_candidates,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                selected = self._select_store_candidate(candidates, requested_store_name)
                if selected is None:
                    raise StoreSelectionRequired(requested_store_name, result.store_candidates)
                # The store-name label itself is not always the card's
                # navigation target: on the current Canvas layout, tapping
                # that label can land on the product grid below and open a
                # product detail page.  Prefer the “进店” button in the same
                # card, falling back to the name only on older layouts.
                entry_bounds = self._store_entry_bounds(selected, store_ocr_detections, xml)
                left, top, right, bottom = entry_bounds or selected
                self.device.tap((left + right) // 2, (top + bottom) // 2)
                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    "04_store",
                    lambda _xml, values: self._is_store_page(values),
                    timeout=10.0,
                    min_settle=0.25,
                )
                ui_path = evidence_dir / "04_store.xml"
                ui_path.write_text(xml, encoding="utf-8")
                screenshot = self.device.screenshot(evidence_dir / "04_store.png")
                ocr_texts = self.ocr.recognize(screenshot) if self.ocr.name != "none" else []
                # In the Web default (OCR provider ``none``), the store header
                # can still be Canvas-only.  The store-result transition has
                # already lazily prepared local RapidOCR when needed; reuse it
                # here so follower counts do not depend on whether the header
                # happened to be exposed in UI XML.
                if not ocr_texts and self.device.is_live:
                    if self._auto_ocr_provider is None and not self._auto_ocr_attempted:
                        self._auto_ocr_attempted = True
                        try:
                            from wen.extract.ocr import RapidOcrProvider

                            self._auto_ocr_provider = RapidOcrProvider()
                        except Exception as exc:  # noqa: BLE001 - keep XML-only extraction usable
                            self._auto_ocr_error = str(exc)
                    recognize_boxes = getattr(self._auto_ocr_provider, "recognize_boxes", None)
                    if callable(recognize_boxes):
                        # Do not derive a product ROI here: the follower count
                        # is in the store header, above the product price nodes.
                        ocr_texts = [item.text for item in recognize_boxes(screenshot)]
                self._guard_page(texts + ocr_texts)
                if not self._is_store_page(texts + ocr_texts):
                    raise RuntimeError("已点击候选店铺，但未确认进入店铺页；已保留现场证据。")
                # The later product-list hierarchy may omit the store header.
                # Capture the verified store metadata now, while its header is
                # definitely on screen, and merge it into the final result.
                verified_store, _unused_products = self.extractor.extract(
                    xml, requested_store_name or keyword, ocr_texts
                )
                if requested_store_name:
                    verified_store.name = requested_store_name

                # 进店后头部会延迟上移，如果在动画前读取坐标、动画后
                # 点击，就会落到商品卡并误入详情页。先主动将排序栏缓慢
                # 滚到吸顶位置，确认坐标稳定后再点击单列/排序控件。
                xml = self._prepare_store_product_view(xml, evidence_dir)
                self._prepare_product_list(sort_mode, xml, evidence_dir)
                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    "05_products",
                    lambda current_xml, values: self._xml_has_selected_label(current_xml, "商品")
                    and bool(DouyinExtractor._title_nodes(parse_ui_xml(current_xml))),
                    timeout=10.0,
                    min_settle=0.25,
                )
                ui_path = evidence_dir / "05_products.xml"
                ui_path.write_text(xml, encoding="utf-8")
                screenshot = self.device.screenshot(evidence_dir / "05_products.png")
                ocr_texts = self._plain_ocr_texts_if_needed(screenshot)
                self._guard_page(texts + ocr_texts)

            ocr_detections = (
                favorite_ocr_detections
                if favorites_mode
                else self._product_ocr_detections(
                    screenshot, evidence_dir, "05_products", xml=xml
                )
            )
            if ocr_detections:
                ocr_texts = list(dict.fromkeys([*ocr_texts, *(item.text for item in ocr_detections)]))
            if favorites_mode and self.device.is_live:
                # 收藏页不是店铺页；不要把页面中偶然出现的店铺名作为结果维度。
                store = None
                products, invalid_titles, favorite_rows = self._extract_favorite_products_with_rows(
                    xml, ocr_detections
                )
                result.invalid_favorite_titles.extend(invalid_titles)
            else:
                # The search keyword may be a shortened alias (for example
                # “鸭鸭童装旗舰店”), while the verified result card/page uses
                # the user's strict full name.  Use that verified name as the
                # extractor hint so the persisted store dimension cannot
                # silently regress to the shorter search keyword.
                store, products = self.extractor.extract(
                    xml,
                    requested_store_name or keyword,
                    ocr_texts,
                    ocr_detections,
                )
                # The product-list route may not repeat the store header, so
                # its fallback name can come from a shortened keyword or an
                # OCR fragment.  The name was already strictly verified on
                # the store page; persist that authoritative value.
                if requested_store_name:
                    store.name = requested_store_name
                if verified_store is not None:
                    if store.douyin_id is None:
                        store.douyin_id = verified_store.douyin_id
                    if store.followers is None:
                        store.followers = verified_store.followers
                    if store.product_count is None:
                        store.product_count = verified_store.product_count
                    existing_field_keys = {field.key for field in store.fields}
                    for field in verified_store.fields:
                        if field.key not in existing_field_keys:
                            store.fields.append(field)
                            existing_field_keys.add(field.key)
                if favorites_mode:
                    store = None
                    products, invalid_titles = self._filter_invalid_favorites(
                        products, texts + ocr_texts, xml
                    )
                    result.invalid_favorite_titles.extend(invalid_titles)
            self._set_state(result, "product_list")
            if favorites_mode and self.device.is_live:
                capture_favorite_product_images(
                    screenshot, products, favorite_rows, evidence_dir
                )
            else:
                capture_product_images(xml, screenshot, products, evidence_dir)
                if self.device.is_live:
                    products = self._drop_incomplete_edge_products(products)
            result.store = store
            target_count = max_products or self.settings.max_products
            if selection_mode == ProductSelectionMode.PRECISE_CATALOG:
                # 精准模式是目录发现，不应只取首屏或 max_products 条；优先使用
                # 店铺页显示的商品总数，未知时再按可配置上限持续翻页。
                target_count = (store.product_count if store else None) or max(target_count, 500)
            products = self._collect_more_pages(
                keyword,
                store,
                products,
                evidence_dir,
                target_count,
                result,
                initial_xml=xml,
                product_selections=requested_selections,
                collect_all=favorites_mode,
                favorites_mode=favorites_mode and self.device.is_live,
                initial_collection_complete=favorite_collection_complete,
            )
            if requested_selections:
                missing_titles = self._missing_requested_titles(products, requested_selections)
                if missing_titles:
                    result.missing_product_titles = missing_titles
                    result.warnings.append(
                        "精准查询未命中 "
                        f"{len(missing_titles)} 个已保存商品，可能是商品已改名或下架："
                        + "、".join(missing_titles)
                    )
            products = self._filter_products(
                products, requested_selections, price_min, price_max, result
            )
            products = self._sort_products(products, sort_mode)
            # 指定商品查询优先保证命中目标；未指定标题时才按最大条数截断。
            result.products = (
                products if requested_selections or favorites_mode else products[:target_count]
            )
            if favorites_mode and result.invalid_favorite_titles:
                result.warnings.append(
                    f"收藏中发现 {len(result.invalid_favorite_titles)} 个失效或不可用商品，已自动排除。"
                )
            if self.device.is_live and not result.products:
                result.warnings.append("当前页面未解析出商品销量字段，可能需要进入店铺商品列表或启用 OCR。")
            elif self.device.is_live and result.products and not any(
                product.displayed_sales is not None for product in result.products
            ):
                result.warnings.append(
                    "商品卡截图中可见“已售”文字，但 UI XML/OCR 未成功绑定到商品；"
                    "请检查商品 OCR 证据或提高截图清晰度。"
                )
            if self._auto_ocr_error and self.device.is_live:
                result.warnings.append(f"商品销量 OCR 未启用：{self._auto_ocr_error}")
            result.status = JobStatus.SUCCEEDED
            self._set_state(result, "completed")
        except LoginRequired as exc:
            result.status = JobStatus.PAUSED
            self._set_state(result, "login_required")
            result.warnings.append(str(exc))
        except RiskControlDetected as exc:
            result.status = JobStatus.PAUSED
            self._set_state(result, "risk_control")
            result.warnings.append(str(exc))
        except StoreSelectionRequired as exc:
            result.status = JobStatus.PAUSED
            self._set_state(result, "store_selection_required")
            result.warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - persist all unexpected page/device failures
            result.status = JobStatus.FAILED
            self._set_state(result, "failed")
            result.errors.append(str(exc))
        finally:
            result.finished_at = datetime.now().astimezone()
            self.store.save_result(result)
            self.device.close()
        return result

    @staticmethod
    def _normalize_product_ids(values: list[str]) -> list[str]:
        normalized: list[str] = []
        invalid: list[str] = []
        for raw in values:
            value = str(raw).strip()
            if not value:
                continue
            if not re.fullmatch(r"\d{6,30}", value):
                invalid.append(value)
            else:
                normalized.append(value)
        if invalid:
            raise ValueError(
                "商品 ID 只能包含 6–30 位数字，以下内容无效：" + "、".join(invalid)
            )
        return normalized

    @staticmethod
    def _product_deep_link(product_id: str) -> str:
        escaped = quote(product_id, safe="")
        # Live probing on the current Douyin build showed that product_id by
        # itself lands on “网络异常”; promotion_id is also required.  For a
        # numeric mall product ID, using the same value for both opens the
        # canonical product detail page.
        return (
            "snssdk1128://ec_goods_detail?"
            f"product_id={escaped}&promotion_id={escaped}"
        )

    @staticmethod
    def _store_deep_link(sec_shop_id: str) -> str:
        return "snssdk1128://goods/store?sec_shop_id=" + quote(sec_shop_id, safe="")

    @staticmethod
    def _is_product_detail_page(xml: str, texts: list[str]) -> bool:
        joined = " ".join(texts)
        has_gallery = any(
            (node.text or node.content_desc).strip() in {"图片1", "商品图片1", "商品图集"}
            for node in parse_ui_xml(xml)
        )
        return has_gallery and (
            "已售" in joined
            or "券后价" in joined
            or "专享价" in joined
            or "加购" in joined
        )

    def _product_detail_page_identity(self, xml: str, product_id: str) -> tuple[object, ...]:
        """Identify the rendered detail route, not merely the detail-page type.

        Consecutive product-ID queries transition from one product detail page
        to another.  During that transition the old page still satisfies
        ``_is_product_detail_page`` and used to be accepted immediately, so its
        XML bounds could be paired with a screenshot from the next rendering
        frame.  Appium's live hierarchy exposes a new Android ``window-id`` for
        every detail route; use that authoritative signal.  The parsed product
        fields are a fixture/compatibility fallback for hierarchies without it.
        """
        window_ids = tuple(sorted(set(re.findall(r'\bwindow-id="(\d+)"', xml))))
        if window_ids:
            return ("window", *window_ids)
        try:
            product = self.extractor.extract_product_detail(xml, product_id)
        except Exception:  # noqa: BLE001 - an incomplete transition has no identity yet
            return ()
        return (
            "content",
            product.title,
            product.price,
            product.displayed_sales_raw,
        )

    def _collect_product_ids(
        self,
        product_ids: list[str],
        evidence_dir: Path,
        result: CollectionResult,
    ) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        previous_detail_identity: tuple[object, ...] = ()
        invalid_markers = (
            "网络异常", "商品已下架", "商品不存在", "商品已删除", "暂不可购买", "访问出错",
        )
        for index, product_id in enumerate(product_ids, start=1):
            stem = f"detail_{index:03d}_{product_id}"
            try:
                self.device.open_uri(
                    self._product_deep_link(product_id), self.settings.douyin_package
                )

                def detail_ready(
                    current_xml: str,
                    values: list[str],
                    requested_product_id: str = product_id,
                    previous_identity: tuple[object, ...] = previous_detail_identity,
                ) -> bool:
                    joined_values = " ".join(values)
                    if any(marker in joined_values for marker in invalid_markers):
                        return True
                    return self._is_product_detail_page(current_xml, values) and (
                        not previous_identity
                        or self._product_detail_page_identity(
                            current_xml, requested_product_id
                        )
                        != previous_identity
                    )

                xml, texts = self._wait_for_ui(
                    evidence_dir,
                    stem,
                    detail_ready,
                    timeout=10.0,
                    min_settle=0.15,
                    poll_interval=0.2,
                )
                (evidence_dir / f"{stem}.xml").write_text(xml, encoding="utf-8")
                joined = " ".join(texts)
                self._guard_page(texts)
                visible_error = next(
                    (marker for marker in invalid_markers if marker in joined), None
                )
                if visible_error:
                    raise RuntimeError(f"详情页显示“{visible_error}”")
                if not self._is_product_detail_page(xml, texts):
                    self.device.screenshot(evidence_dir / f"{stem}_failed.png")
                    raise RuntimeError("10 秒内未确认进入商品详情页")

                # Record the accepted route before screenshotting.  The next
                # product must expose a genuinely new route rather than merely
                # remaining on this already-valid detail page.
                previous_detail_identity = self._product_detail_page_identity(
                    xml, product_id
                )

                screenshot = self.device.screenshot(evidence_dir / f"{stem}.png")
                product = self.extractor.extract_product_detail(xml, product_id)
                if product.price is None:
                    # This fallback is intentionally rare.  Normal product
                    # pages expose the actionable bottom-button price in XML;
                    # only an XML-missing price runs one screenshot OCR pass.
                    detections = self._product_ocr_detections(
                        screenshot, evidence_dir, f"{stem}_price", xml=xml
                    )
                    product = self.extractor.extract_product_detail(
                        xml, product_id, detections
                    )
                if product.price is None:
                    raise RuntimeError("详情页已打开，但没有读取到可成交价格")
                product.position = index
                capture_product_detail_image(
                    xml, screenshot, product, evidence_dir
                )
                products.append(product)
            except Exception as exc:  # noqa: BLE001 - one bad ID must not discard the others
                result.failed_product_ids.append(product_id)
                result.warnings.append(f"商品 ID {product_id} 读取失败：{exc}")
        return products

    def _set_state(self, result: CollectionResult, state: str) -> None:
        result.state = state
        self.store.update_job_state(result.job_id, result.status, state)

    def _collect_more_pages(
        self,
        keyword: str,
        store,
        initial_products,
        evidence_dir: Path,
        target_count: int,
        result: CollectionResult,
        initial_xml: str | None = None,
        product_selections: list[ProductSelection] | None = None,
        collect_all: bool = False,
        favorites_mode: bool = False,
        initial_collection_complete: bool = False,
    ):
        requested_selections = product_selections or []
        if requested_selections and self._requested_products_found(
            initial_products, requested_selections
        ):
            return initial_products
        if favorites_mode and initial_collection_complete and not self._favorite_needs_completion(
            initial_products
        ):
            # The first screen already contains the complete collection plus the
            # recommendation marker.  Scrolling would move into unrelated cards.
            return initial_products
        if not collect_all and not requested_selections and len(initial_products) >= target_count:
            return initial_products
        products = list(initial_products)
        previous_signature: tuple[str, ...] | None = None
        previous_count = len(products)
        stalled_rounds = 0
        previous_page_products: list[ProductRecord] = list(initial_products)
        previous_page_keys: tuple[tuple[str, float | None, str | None, str | None], ...] = ()
        device_info = self.device.info()
        width = device_info.screen_width or 1080
        height = device_info.screen_height or 2400
        page_number = 0
        settled_xml = initial_xml
        for page_number in range(1, self.settings.max_scrolls + 1):
            # Plan movement from the visible card pitch instead of using a
            # fixed 80%-screen swipe. The latter can jump across two cards when
            # the header/list geometry changes or the page is still animating.
            before_path = evidence_dir / f".scroll_{page_number:02d}_before.xml"
            if settled_xml:
                # Screenshot/OCR do not mutate the phone. The settled XML from
                # the preceding viewport is therefore exactly the hierarchy
                # needed to plan the next gesture; a fresh device dump here was
                # redundant and added roughly 0.6–1.1 seconds per page.
                before_xml = settled_xml
                before_path.write_text(before_xml, encoding="utf-8")
            else:
                before_path = self.device.dump_ui(before_path)
                before_xml = before_path.read_text(encoding="utf-8")
            expected_scroll_delta = self._planned_product_scroll_delta(
                before_xml, height
            )
            # The boolean is only a backend hint on Canvas pages; end-of-list
            # is decided from settled content/stall checks below.
            self._scroll_product_list(before_xml, width, height)
            page_xml_path = evidence_dir / f"scroll_{page_number:02d}.xml"
            page_xml, page_texts, current_signature = self._wait_for_product_scroll(
                page_xml_path,
                before_xml,
                timeout=4.0,
            )
            settled_xml = page_xml
            self._guard_page(page_texts)
            if not DouyinExtractor._title_nodes(parse_ui_xml(page_xml)):
                raise RuntimeError(
                    "滚动后当前页面已不再是抖音商品列表，已停止采集并保留本次滚动截图；"
                    "可能触发了系统手势区或页面发生了跳转。"
                )
            signature = current_signature
            page_screenshot = self.device.screenshot(evidence_dir / f"scroll_{page_number:02d}.png")
            page_ocr_detections = self._product_ocr_detections(
                page_screenshot,
                evidence_dir,
                f"scroll_{page_number:02d}",
                xml=page_xml,
            )
            page_ocr_texts = [item.text for item in page_ocr_detections]
            if favorites_mode:
                # Favorite cards expose a full-width “删除” action in XML. Once
                # those rows disappear, the screen is the recommendation grid;
                # never parse or continue through it.
                if not self._favorite_card_bounds(page_xml):
                    break
                page_products, invalid_titles, page_favorite_rows = (
                    self._extract_favorite_products_with_rows(
                        page_xml,
                        page_ocr_detections,
                        defer_leading_untitled=True,
                    )
                )
                for title in invalid_titles:
                    if title not in result.invalid_favorite_titles:
                        result.invalid_favorite_titles.append(title)
            else:
                _page_store, page_products = self.extractor.extract(
                    page_xml, keyword, page_ocr_texts, page_ocr_detections
                )
                if collect_all:
                    page_products, invalid_titles = self._filter_invalid_favorites(
                        page_products, page_texts + page_ocr_texts, page_xml
                    )
                    for title in invalid_titles:
                        if title not in result.invalid_favorite_titles:
                            result.invalid_favorite_titles.append(title)
            if favorites_mode:
                capture_favorite_product_images(
                    page_screenshot, page_products, page_favorite_rows, evidence_dir
                )
            else:
                capture_product_images(page_xml, page_screenshot, page_products, evidence_dir)
            current_page_products = list(page_products)
            page_keys = tuple(self._product_observation_key(product) for product in current_page_products)
            if page_keys and page_keys == previous_page_keys:
                # A swipe that did not move the list is not a second business
                # row. Do not let a stalled viewport multiply every card.
                page_products = []
            else:
                # Remove only the physical suffix/prefix overlap caused by
                # two adjacent screenshots. This is not a global title/price
                # de-duplication rule: cards elsewhere in the list, including
                # same-title cards, are appended independently.
                overlap = self._viewport_overlap_count(
                    previous_page_products,
                    current_page_products,
                    before_xml=before_xml,
                    current_xml=page_xml,
                    expected_scroll_delta=expected_scroll_delta,
                )
                if overlap:
                    page_products = self._products_after_viewport_overlap(
                        products,
                        previous_page_products,
                        current_page_products,
                        overlap,
                    )
                else:
                    page_products = current_page_products
                if self.device.is_live and not favorites_mode:
                    # 页面边缘只露出一部分的卡片不是一条完整观测。
                    # 不做图片/价格补丁式拼接，本屏忽略，下一屏完整显示时再收录。
                    page_products = self._drop_incomplete_edge_products(page_products)
            for product in page_products:
                # No business-level de-duplication or observation merging:
                # every card emitted by the current viewport is an independent
                # result row, even when title and price are identical.
                product.position = len(products) + 1
                products.append(product)
            if page_keys:
                previous_page_products = current_page_products
                previous_page_keys = page_keys
            if signature == previous_signature and len(products) == previous_count:
                # 一次 swipe 可能仍在动画中；允许再试一次，避免把首屏误判成目录末尾。
                stalled_rounds += 1
            else:
                stalled_rounds = 0
            previous_signature = signature
            previous_count = len(products)
            if favorites_mode and self._favorite_collection_end_seen(
                page_xml, page_ocr_detections
            ):
                break
            if stalled_rounds >= 2:
                break
            if requested_selections and self._requested_products_found(
                products, requested_selections
            ):
                break
            if not collect_all and not requested_selections and len(products) >= target_count:
                break
        if not products:
            result.warnings.append("滚动后仍未发现可解析商品。")
        elif self.device.is_live and not any(product.image_path for product in products):
            result.warnings.append("已读取商品文字，但未能从商品卡定位主图；已保留整屏截图和 UI XML 供排查。")
        if not requested_selections and len(products) < target_count and page_number >= self.settings.max_scrolls:
            if collect_all:
                result.warnings.append(
                    f"已读取 {len(products)} 条有效收藏商品，达到滚动上限 {self.settings.max_scrolls}；"
                    "如收藏较多，可提高滚动上限后继续读取。"
                )
            else:
                result.warnings.append(
                    f"已读取 {len(products)} 条商品，但未达到店铺显示的 {target_count} 条；"
                    "可能需要提高滚动上限或页面未继续加载。"
                )
        return products

    def _scroll_product_list(self, xml: str, width: int, height: int) -> bool | None:
        """Scroll the product viewport by a geometry-derived bounded amount."""
        top, area_height, desired_delta = self._product_scroll_geometry(xml, height)
        percent = max(0.30, min(0.72, desired_delta / area_height))
        return self.device.scroll(
            0,
            top,
            width,
            area_height,
            direction="down",
            percent=percent,
            speed=1200,
        )

    @classmethod
    def _planned_product_scroll_delta(cls, xml: str, height: int) -> int:
        """Return the intended list movement in screen pixels."""
        _top, _area_height, desired_delta = cls._product_scroll_geometry(xml, height)
        return desired_delta

    @staticmethod
    def _product_scroll_geometry(xml: str, height: int) -> tuple[int, int, int]:
        """Calculate a bounded three-card gesture from the visible card geometry."""
        title_nodes = DouyinExtractor._title_nodes(parse_ui_xml(xml))
        tops = [bounds[1] for _title, bounds in title_nodes]
        gaps = [right - left for left, right in pairwise(tops) if right - left >= 120]
        pitch = statistics.median(gaps) if gaps else max(360, round(height * 0.2))
        top = max(0, min(tops or [round(height * 0.25)]) - 180)
        # Keep the gesture rectangle away from Android's bottom navigation/
        # gesture handle. Starting at the last 20px can open the recent-apps
        # screen instead of scrolling the product list on gesture-navigation
        # devices such as the Redmi K80 Ultra.
        bottom = max(top + 600, height - max(180, round(height * 0.08)))
        area_height = bottom - top
        # Move about three card heights. A normal viewport still retains
        # roughly one complete card as overlap, which is enough to verify
        # continuity while reducing the number of screenshots/OCR passes.
        desired_delta = max(
            round(area_height * 0.35),
            min(round(pitch * 3.0), round(area_height * 0.72)),
        )
        return top, area_height, desired_delta

    def _wait_for_product_scroll(
        self,
        page_xml_path: Path,
        before_xml: str,
        *,
        timeout: float,
        poll_interval: float = 0.18,
    ) -> tuple[str, list[str], tuple[str, ...]]:
        """Wait for two identical post-scroll title layouts before OCR capture."""
        deadline = time.monotonic() + timeout
        before_signature = self._product_title_signature(before_xml)
        latest_xml = before_xml
        latest_texts = extract_texts(before_xml)
        latest_signature = before_signature
        previous_signature: tuple[str, ...] | None = None
        stable_rounds = 0
        while True:
            try:
                page_xml_path = self.device.dump_ui(page_xml_path)
                latest_xml = page_xml_path.read_text(encoding="utf-8")
                latest_texts = extract_texts(latest_xml)
                latest_signature = self._product_title_signature(latest_xml)
            except Exception as exc:  # noqa: BLE001 - keep the last usable hierarchy
                logger.debug("waiting for a stable product list hierarchy failed: %s", exc)
            moved_to_valid_layout = bool(latest_signature) and latest_signature != before_signature
            if moved_to_valid_layout:
                # ADB's slow coordinate swipe returns after the pointer gesture;
                # Appium page_source additionally waits for the short UI idle
                # window. Live measurements across consecutive scrolls showed
                # the first valid hierarchy already identical to the second.
                break
            if latest_signature == previous_signature:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_signature = latest_signature
            # At the end of the list the signature may equal the pre-scroll
            # state. Two identical samples are still a valid settled result.
            # Two equal post-scroll layouts are enough: the ADB swipe command
            # itself returns only after the pointer gesture has completed.
            # The old threshold of 2 required three full page-source reads.
            if latest_signature and stable_rounds >= 1:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        page_xml_path.write_text(latest_xml, encoding="utf-8")
        return latest_xml, latest_texts, latest_signature

    @staticmethod
    def _product_title_signature(xml: str) -> tuple[str, ...]:
        return tuple(
            f"{DouyinExtractor._title_key(title)}@{bounds[1]}:{bounds[3]}"
            for title, bounds in DouyinExtractor._title_nodes(parse_ui_xml(xml))
        )

    @classmethod
    def _viewport_overlap_count(
        cls,
        previous: list[ProductRecord],
        current: list[ProductRecord],
        *,
        before_xml: str | None = None,
        current_xml: str | None = None,
        expected_scroll_delta: int | None = None,
    ) -> int:
        """Find only an adjacent screenshot's physical suffix/prefix overlap."""
        if not previous or not current:
            return 0
        # Identical adjacent listings are valid independent products.  Their
        # title/price/image/sales fingerprints can be completely equal, so a
        # text-only suffix/prefix comparison cannot tell whether the first card
        # after scrolling is the old card or its identical neighbour.  Resolve
        # that ambiguity from the hierarchy immediately before the gesture and
        # the intended physical movement.
        if before_xml and current_xml and expected_scroll_delta:
            physical_overlap = cls._physical_viewport_overlap_count(
                previous,
                current,
                before_xml,
                current_xml,
                expected_scroll_delta,
            )
            if physical_overlap is not None:
                return physical_overlap
        previous_keys = [cls._product_observation_key(product) for product in previous]
        current_keys = [cls._product_observation_key(product) for product in current]
        maximum = min(len(previous_keys), len(current_keys), 6)
        for count in range(maximum, 0, -1):
            if previous_keys[-count:] == current_keys[:count]:
                return count
        return 0

    @classmethod
    def _physical_viewport_overlap_count(
        cls,
        previous: list[ProductRecord],
        current: list[ProductRecord],
        before_xml: str,
        current_xml: str,
        expected_scroll_delta: int,
    ) -> int | None:
        before_cards = cls._xml_card_observations(before_xml)
        current_cards = cls._xml_card_observations(current_xml)
        if not before_cards or not current_cards:
            return None

        previous_keys = [cls._product_card_key(product) for product in previous]
        before_keys = [(title, price) for title, price, _top in before_cards]
        # Locate the last already-recorded physical card in the hierarchy that
        # existed just before the gesture.  The page header can collapse between
        # the prior screenshot and this dump, revealing extra cards without a
        # user scroll, so the two viewports are aligned as ordered sequences.
        previous_alignment = cls._best_contiguous_card_alignment(
            previous_keys, before_keys
        )
        if previous_alignment is None:
            return None
        _previous_start, before_start, matched_count = previous_alignment
        known_through = before_start + matched_count - 1

        # Locate the new viewport's first card in the pre-gesture hierarchy.
        # When several adjacent cards have identical data, select the candidate
        # whose coordinate displacement is closest to the planned gesture.
        current_start = cls._current_start_in_before_viewport(
            before_cards,
            current_cards,
            expected_scroll_delta,
        )
        if current_start is None:
            return None
        overlap = known_through - current_start + 1
        return max(0, min(overlap, len(current)))

    @classmethod
    def _xml_card_observations(
        cls, xml: str
    ) -> list[tuple[str, float | None, int]]:
        nodes = parse_ui_xml(xml)
        title_nodes = DouyinExtractor._title_nodes(nodes)
        products = DouyinExtractor._products(extract_texts(xml), nodes, [])
        products_by_card = {
            product.position - 1: product
            for product in products
            if product.position is not None
        }
        observations: list[tuple[str, float | None, int]] = []
        for card_index, (title, bounds) in enumerate(title_nodes):
            product = products_by_card.get(card_index)
            observations.append(
                (
                    DouyinExtractor._title_key(title),
                    product.price if product else None,
                    bounds[1],
                )
            )
        return observations

    @staticmethod
    def _card_keys_compatible(
        first: tuple[str, float | None], second: tuple[str, float | None]
    ) -> bool:
        return first[0] == second[0] and (
            first[1] is None or second[1] is None or first[1] == second[1]
        )

    @classmethod
    def _best_contiguous_card_alignment(
        cls,
        left: list[tuple[str, float | None]],
        right: list[tuple[str, float | None]],
    ) -> tuple[int, int, int] | None:
        best: tuple[int, int, int] | None = None
        best_score: tuple[int, int, int] | None = None
        for left_start in range(len(left)):
            for right_start in range(len(right)):
                count = 0
                while (
                    left_start + count < len(left)
                    and right_start + count < len(right)
                    and cls._card_keys_compatible(
                        left[left_start + count], right[right_start + count]
                    )
                ):
                    count += 1
                if not count:
                    continue
                score = (
                    count,
                    left_start + count,
                    right_start + count,
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best = (left_start, right_start, count)
        return best

    @classmethod
    def _current_start_in_before_viewport(
        cls,
        before: list[tuple[str, float | None, int]],
        current: list[tuple[str, float | None, int]],
        expected_scroll_delta: int,
    ) -> int | None:
        best_start: int | None = None
        best_score: tuple[float, int] | None = None
        tolerance = max(180, expected_scroll_delta * 0.55)
        for before_start in range(len(before)):
            count = 0
            deltas: list[int] = []
            while (
                before_start + count < len(before)
                and count < len(current)
                and cls._card_keys_compatible(
                    (before[before_start + count][0], before[before_start + count][1]),
                    (current[count][0], current[count][1]),
                )
            ):
                deltas.append(
                    before[before_start + count][2] - current[count][2]
                )
                count += 1
            if not count:
                continue
            median_delta = float(statistics.median(deltas))
            if median_delta <= 0 or abs(median_delta - expected_scroll_delta) > tolerance:
                continue
            # Repeated adjacent cards can have the same title and price. In that
            # case a longer textual match may begin one card too early and erase
            # a real business row. Physical movement is therefore authoritative;
            # match length is only the tie-breaker.
            score = (-abs(median_delta - expected_scroll_delta), count)
            if best_score is None or score > best_score:
                best_score = score
                best_start = before_start
        return best_start

    @staticmethod
    def _product_card_key(product: ProductRecord) -> tuple[str, float | None]:
        return DouyinExtractor._title_key(product.title), product.price

    @staticmethod
    def _complete_product_observation(
        target: ProductRecord, source: ProductRecord
    ) -> None:
        """Fill a clipped card from its complete observation on the next screen."""
        copied_field_keys: set[str] = set()
        for attribute in (
            "price",
            "original_price",
            "displayed_sales",
            "displayed_sales_raw",
            "rating",
            "review_count",
            "product_id",
            "source_url",
        ):
            if getattr(target, attribute) is None and getattr(source, attribute) is not None:
                setattr(target, attribute, getattr(source, attribute))
                copied_field_keys.add(attribute)
        # Overlap completion may fill a missing image, but must never replace a
        # full crop already captured in the prior viewport. The same card at
        # the top of the next screenshot can sit underneath the sticky Tab.
        if source.image_path and not target.image_path:
            target.image_path = source.image_path
        existing_fields = {
            (field.key, field.raw_value, str(field.value)) for field in target.fields
        }
        for field in source.fields:
            identity = (field.key, field.raw_value, str(field.value))
            if identity not in existing_fields and (
                field.key in copied_field_keys
                or not any(existing.key == field.key for existing in target.fields)
            ):
                target.fields.append(field.model_copy(deep=True))
                existing_fields.add(identity)

    @classmethod
    def _products_after_viewport_overlap(
        cls,
        collected: list[ProductRecord],
        previous_viewport: list[ProductRecord],
        current_viewport: list[ProductRecord],
        overlap: int,
    ) -> list[ProductRecord]:
        """Remove only overlap cards that were actually persisted previously.

        A bottom-edge card can be visible in the previous hierarchy but be
        deliberately deferred because its image is incomplete. When that card
        appears fully at the top of the next viewport it is physical overlap,
        but it is still a new result row and must not be skipped. Object identity
        tracks whether that exact prior observation entered ``collected``;
        titles, prices and images never participate in this decision.
        """
        collected_ids = {id(product) for product in collected}
        retained_prefix: list[ProductRecord] = []
        previous_suffix = previous_viewport[-overlap:]
        current_prefix = current_viewport[:overlap]
        for old_product, complete_product in zip(
            previous_suffix, current_prefix, strict=True
        ):
            if id(old_product) in collected_ids:
                cls._complete_product_observation(old_product, complete_product)
            else:
                retained_prefix.append(complete_product)
        return [*retained_prefix, *current_viewport[overlap:]]

    @classmethod
    def _product_observation_key(
        cls, product: ProductRecord
    ) -> tuple[str, float | None, str | None, str | None]:
        return (
            DouyinExtractor._title_key(product.title),
            product.price,
            product.image_path,
            product.displayed_sales_raw,
        )

    @staticmethod
    def _favorite_card_bounds(xml: str) -> list[tuple[int, int, int, int]]:
        """Return the visible collection-card rows exposed by the 删除 actions.

        On the real 商品收藏 page, product titles/images are canvas-rendered, but
        each user's card still has a full-row 删除 action in the accessibility tree.
        Recommendation cards below 你可能还会喜欢 do not have that action.
        """
        bounds: set[tuple[int, int, int, int]] = set()
        for node in parse_ui_xml(xml):
            value = (node.text or node.content_desc or "").strip()
            if node.bounds and "删除" in value:
                bounds.add(node.bounds)
        return sorted(bounds, key=lambda item: (item[1], item[0]))

    @staticmethod
    def _favorite_needs_completion(products: list[ProductRecord]) -> bool:
        """Whether the last visible card may be cut off at the bottom edge."""
        if not products:
            return False
        last = products[-1]
        return last.price is None or last.displayed_sales is None

    @staticmethod
    def _favorite_collection_end_seen(
        xml: str, detections: list[OcrDetection] | None = None
    ) -> bool:
        values = extract_texts(xml) + [item.text for item in (detections or [])]
        return any(marker in value for value in values for marker in _FAVORITE_END_MARKERS)

    @staticmethod
    def _favorite_title(detections: list[OcrDetection]) -> str | None:
        """Pick the title line from one full-width favorite card.

        The title is normally the first substantial right-hand OCR line. Badges,
        prices, sales, invalid-state text and marketing labels are intentionally
        excluded before choosing it.
        """
        excluded = (
            "已失效", "商品失效", "已下架", "删除", "已售", "销量", "销售", "售出",
            "好评", "运费险", "补贴", "满减", "券后价", "优惠", "加购", "直播",
            "旗舰", "店铺", "团购", "分钟达", "小时达", "价格", "新人价",
        )
        candidates: list[OcrDetection] = []
        for detection in detections:
            text = re.sub(r"\s+", "", detection.text or "").strip()
            if len(text) < 6 or any(marker in text for marker in excluded):
                continue
            if parse_price(text) is not None or _FAVORITE_SALES_RE.search(text):
                continue
            candidates.append(
                OcrDetection(text=text, bounds=detection.bounds, confidence=detection.confidence)
            )
        if not candidates:
            return None
        # Product titles sit to the right of the thumbnail and above prices. OCR
        # order is not guaranteed, so use coordinates, with a right-hand preference.
        selected = min(
            candidates,
            key=lambda item: (
                0 if item.bounds[0] >= 250 else 1,
                item.bounds[1],
                -len(item.text),
            ),
        )
        return selected.text

    @classmethod
    def _extract_favorite_products(
        cls, xml: str, detections: list[OcrDetection]
    ) -> tuple[list[ProductRecord], list[str]]:
        products, invalid_titles, _rows = cls._extract_favorite_products_with_rows(
            xml, detections
        )
        return products, invalid_titles

    @classmethod
    def _extract_favorite_products_with_rows(
        cls,
        xml: str,
        detections: list[OcrDetection],
        *,
        defer_leading_untitled: bool = False,
    ) -> tuple[list[ProductRecord], list[str], list[tuple[int, int, int, int]]]:
        """Extract only cards above the recommendation section.

        This path deliberately does not feed recommendation OCR into the generic
        shop extractor. It uses the real page's card rows and associates title,
        price and sales detections within each row.  The third return value keeps
        the matching row bounds for screenshot-based thumbnail cropping.
        """
        products: list[ProductRecord] = []
        invalid_titles: list[str] = []
        product_rows: list[tuple[int, int, int, int]] = []
        rows = cls._favorite_card_bounds(xml)
        row_heights = [bottom - top for _left, top, _right, bottom in rows]
        typical_height = statistics.median(row_heights) if row_heights else 0
        row_tops = [
            top - min(80, round(typical_height * 0.25))
            for _left, top, _right, _bottom in rows
        ]
        for row_index, row in enumerate(rows):
            _left, top, _right, bottom = row
            # The first/last accessibility row may be only the clipped tail of
            # a card crossing the viewport boundary. Do not merge it into a
            # neighboring product or persist a title-only duplicate; the same
            # physical card is collected once it is fully visible next screen.
            if typical_height and bottom - top < typical_height * 0.72:
                continue
            # The accessibility ``删除`` action starts a little below the
            # canvas card's visual top.  On the live page the valid title began
            # 31px above its row (y=753 vs y=784).  Include that small header
            # strip, otherwise a completely visible favorite has no title and
            # is silently discarded.
            row_top = row_tops[row_index]
            # Accessibility action rows overlap slightly.  Without an explicit
            # boundary, the clipped invalid row at the top of a scrolled page
            # can steal the next valid card's title.  The next card's adjusted
            # top is the physical boundary between their OCR observations.
            row_bottom = (
                min(bottom, row_tops[row_index + 1])
                if row_index + 1 < len(row_tops)
                else bottom
            )
            row_detections = [
                item
                for item in detections
                if row_top <= (item.bounds[1] + item.bounds[3]) // 2 < row_bottom
            ]
            title = cls._favorite_title(row_detections)
            # A title whose bounding box starts above the row is the clipped
            # tail of the card from the previous screen, not a new product.
            if title and cls._favorite_title_is_partial(title, row_detections, row_top):
                title = None
            row_texts = [item.text for item in row_detections]
            invalid = any(
                marker in text for text in row_texts for marker in _FAVORITE_INVALID_MARKERS
            )
            if invalid:
                # Adjacent screenshots deliberately overlap.  An untitled
                # invalid first row on a scrolled page is the tail already
                # counted on the preceding viewport, not a fourth product.
                if defer_leading_untitled and row_index == 0 and not title:
                    continue
                invalid_titles.append(title or "收藏页中的失效商品（标题未能读取）")
                continue
            if not title:
                continue

            price_detection: OcrDetection | None = None
            price: float | None = None
            sales_detection: OcrDetection | None = None
            sales_raw: str | None = None
            for item in row_detections:
                parsed_price = parse_price(item.text)
                if parsed_price is not None and price_detection is None:
                    price_detection = item
                    price = parsed_price
                sales_match = _FAVORITE_SALES_RE.search(item.text)
                if sales_match and sales_detection is None:
                    sales_detection = item
                    sales_raw = sales_match.group(1).strip()

            fields: list[FieldValue] = []
            if price is not None and price_detection is not None:
                fields.append(
                    FieldValue(
                        key="price",
                        value=price,
                        raw_value=price_detection.text,
                        method="ocr",
                        confidence=price_detection.confidence,
                    )
                )
            if sales_raw is not None and sales_detection is not None:
                fields.append(
                    FieldValue(
                        key="displayed_sales",
                        value=parse_count(sales_raw),
                        raw_value=sales_raw,
                        method="ocr",
                        confidence=sales_detection.confidence,
                    )
                )
            products.append(
                ProductRecord(
                    title=title,
                    price=price,
                    displayed_sales=parse_count(sales_raw),
                    displayed_sales_raw=sales_raw,
                    position=len(products) + 1,
                    fields=fields,
                )
            )
            product_rows.append(row)
        return products, invalid_titles, product_rows

    @classmethod
    def _merge_favorite_partial_row(
        cls,
        products: list[ProductRecord],
        xml: str,
        detections: list[OcrDetection],
    ) -> bool:
        """Attach price/sales seen after a card crossed a swipe boundary.

        A favorite card can render its title on the bottom of one screenshot and
        its price/sales on the top of the next.  The next page then has a valid
        ``删除`` row but no title, so the regular title-first parser intentionally
        skips it.  Its first row is nevertheless unambiguously the continuation
        of the last product collected so far; fill only fields missing on that
        product and leave later recommendation rows untouched.
        """
        if not products:
            return False
        rows = cls._favorite_card_bounds(xml)
        if not rows:
            return False
        first_row = rows[0]
        _left, top, _right, bottom = first_row
        row_detections = [
            item
            for item in detections
            if top <= (item.bounds[1] + item.bounds[3]) // 2 < bottom
        ]
        title = cls._favorite_title(row_detections)
        if title and not cls._favorite_title_is_partial(title, row_detections, top):
            return False
        last = products[-1]
        updated = False
        price_detection: OcrDetection | None = None
        sales_detection: OcrDetection | None = None
        sales_raw: str | None = None
        price: float | None = None
        for item in row_detections:
            parsed_price = parse_price(item.text)
            if parsed_price is not None and price is None:
                price = parsed_price
                price_detection = item
            sales_match = _FAVORITE_SALES_RE.search(item.text)
            if sales_match and sales_raw is None:
                sales_raw = sales_match.group(1).strip()
                sales_detection = item
        if price is not None and last.price is None:
            last.price = price
            if price_detection is not None:
                last.fields.append(
                    FieldValue(
                        key="price",
                        value=price,
                        raw_value=price_detection.text,
                        method="ocr",
                        confidence=price_detection.confidence,
                    )
                )
            updated = True
        if sales_raw is not None and last.displayed_sales is None:
            last.displayed_sales = parse_count(sales_raw)
            last.displayed_sales_raw = sales_raw
            if sales_detection is not None:
                last.fields.append(
                    FieldValue(
                        key="displayed_sales",
                        value=last.displayed_sales,
                        raw_value=sales_raw,
                        method="ocr",
                        confidence=sales_detection.confidence,
                    )
                )
            updated = True
        return updated

    @staticmethod
    def _favorite_title_is_partial(
        title: str, detections: list[OcrDetection], row_top: int
    ) -> bool:
        needle = re.sub(r"\s+", "", title or "")
        return any(
            needle == re.sub(r"\s+", "", item.text or "")
            and item.bounds[1] < row_top
            for item in detections
        )

    @staticmethod
    def _filter_invalid_favorites(products, page_texts: list[str], xml: str | None = None):
        """Remove unavailable favorite cards while preserving a diagnostic title/count."""
        invalid_titles: list[str] = []
        invalid_seen = any(
            marker in text for text in page_texts for marker in _FAVORITE_INVALID_MARKERS
        )
        # Keep invalid state by visible-card occurrence, never by title.  A
        # valid and an expired favorite may legitimately share the same name.
        invalid_flags_by_title: dict[str, list[bool]] = {}
        if xml and invalid_seen:
            try:
                nodes = parse_ui_xml(xml)
                title_nodes = DouyinExtractor._title_nodes(nodes)
                for title, _bounds in title_nodes:
                    invalid_flags_by_title.setdefault(
                        DouyinExtractor._title_key(title), []
                    ).append(False)
                for node in nodes:
                    value = (node.text or node.content_desc or "").strip()
                    if not node.bounds or not any(
                        marker in value for marker in _FAVORITE_INVALID_MARKERS
                    ):
                        continue
                    associated = DouyinExtractor._nearest_title_above(
                        node.bounds, title_nodes, max_gap=650
                    )
                    if associated:
                        key = DouyinExtractor._title_key(associated[0])
                        title_occurrence = sum(
                            1
                            for title, bounds in title_nodes
                            if DouyinExtractor._title_key(title) == key
                            and (bounds[1], bounds[0]) < (associated[1][1], associated[1][0])
                        )
                        flags = invalid_flags_by_title.get(key, [])
                        if title_occurrence < len(flags):
                            flags[title_occurrence] = True
            except (ValueError, TypeError):
                # Keep text-only fallback if a vendor UI dump is malformed.
                invalid_flags_by_title.clear()
        valid_products = []
        product_occurrences: dict[str, int] = {}
        for product in products:
            key = DouyinExtractor._title_key(product.title)
            occurrence = product_occurrences.get(key, 0)
            product_occurrences[key] = occurrence + 1
            flags = invalid_flags_by_title.get(key, [])
            if (
                (occurrence < len(flags) and flags[occurrence])
                or any(marker in product.title for marker in _FAVORITE_INVALID_MARKERS)
            ):
                invalid_titles.append(product.title)
            else:
                valid_products.append(product)
        # The marker is sometimes a sibling node and cannot be safely attached to a card.
        # Keep a generic diagnostic rather than silently dropping a neighboring valid card.
        if invalid_seen and len(valid_products) == len(products):
            invalid_titles.append("收藏页中的失效商品（标题未能读取）")
        return valid_products, invalid_titles

    def _prepare_store_product_view(self, xml: str, evidence_dir: Path) -> str:
        """Select the store product tab and pin its controls before tapping them.

        The current Douyin store page exposes product/sort controls in XML,
        but their y coordinate moves by about 725 px when the header collapses.
        A coordinate resolved before that transition can therefore hit a
        product card.  Refresh once, avoid re-clicking an already selected
        product tab, then perform one slow, geometry-derived header scroll.
        """
        live_path = self.device.dump_ui(evidence_dir / ".04_product_controls_live.xml")
        current_xml = live_path.read_text(encoding="utf-8")
        current_texts = extract_texts(current_xml)
        self._guard_page(current_texts)

        if not self._xml_has_selected_label(current_xml, "商品"):
            product_bounds = find_exact_text_bounds(current_xml, "商品")
            if not product_bounds:
                raise RuntimeError("已进入店铺页，但未找到“商品”标签。")
            self._tap_bounds(product_bounds)
            current_xml, current_texts = self._wait_for_ui(
                evidence_dir,
                "04_product_tab",
                lambda value, _texts: self._xml_has_selected_label(value, "商品")
                and self._sort_control_bounds(value, "综合") is not None,
                timeout=5.0,
                min_settle=0.1,
            )
            self._guard_page(current_texts)

        sort_bounds = self._sort_control_bounds(current_xml, "综合")
        if not sort_bounds:
            raise RuntimeError("已进入店铺商品页，但未找到排序栏。")

        width, height = self._xml_viewport_size(current_xml)
        sticky_top = round(height * 0.168)
        expanded_threshold = round(height * 0.28)
        if sort_bounds[1] > expanded_threshold:
            # Use a slow drag rather than a short fling. On the Redmi K80
            # Ultra, a 350ms swipe overshot the collapsing header and clipped
            # the first card; ~900ms moved the same first card intact beneath
            # the pinned controls.
            delta = max(1, sort_bounds[1] - sticky_top)
            start_y = min(height - 220, round(height * 0.72))
            end_y = max(sticky_top + 180, start_y - delta)
            duration_ms = max(750, min(1100, round(delta / 0.8)))
            self.device.swipe(
                width // 2,
                start_y,
                width // 2,
                end_y,
                duration_ms,
            )
            current_xml, current_texts = self._wait_for_ui(
                evidence_dir,
                "04_product_controls_pinned",
                lambda value, _texts: (
                    (bounds := self._sort_control_bounds(value, "综合")) is not None
                    and bounds[1] <= sticky_top + 90
                    and bool(DouyinExtractor._title_nodes(parse_ui_xml(value)))
                ),
                timeout=4.0,
                min_settle=0.1,
                poll_interval=0.15,
            )
            self._guard_page(current_texts)
            pinned = self._sort_control_bounds(current_xml, "综合")
            if not pinned or pinned[1] > sticky_top + 90:
                raise RuntimeError(
                    "店铺商品排序栏未能稳定吸顶，已停止后续点击以避免误入商品详情。"
                )

        (evidence_dir / "04_product_controls.xml").write_text(
            current_xml, encoding="utf-8"
        )
        return current_xml

    def _prepare_product_list(
        self,
        sort_mode: ProductSortMode,
        xml: str,
        evidence_dir: Path,
    ) -> None:
        """在已吸顶的稳定坐标上切换单列和原生排序。"""
        current_xml = xml
        # The mode label describes the current layout. Only a visible “双列”
        # needs a toggle; “单列” means the desired layout is already active.
        dual_bounds = find_exact_text_bounds(current_xml, "双列")
        if dual_bounds:
            self._tap_bounds(dual_bounds)
            current_xml, _texts = self._wait_for_ui(
                evidence_dir,
                "04_single_column",
                lambda value, _values: find_exact_text_bounds(value, "单列") is not None,
                timeout=4.0,
                min_settle=0.1,
            )

        label = {
            ProductSortMode.COMPREHENSIVE: "综合",
            ProductSortMode.SALES: "销量",
            ProductSortMode.NEWEST: "上新",
            ProductSortMode.PRICE_ASC: "价格",
            ProductSortMode.PRICE_DESC: "价格",
        }[sort_mode]
        sort_bounds = self._sort_control_bounds(current_xml, label)
        if not sort_bounds:
            raise RuntimeError(f"已进入商品列表，但未找到“{label}”排序控件。")
        self._tap_bounds(sort_bounds)
        if sort_mode == ProductSortMode.PRICE_DESC:
            # 抖音的“价格”按钮第一次进入价格升序，第二次才切到降序。
            # 等第一次点击被页面接收后再点一次；最终结果仍完全沿用页面顺序。
            time.sleep(0.35)
            self._tap_bounds(sort_bounds)
        # The following predicate verifies the resulting product layout. A
        # tiny settle only prevents reading the pre-tap frame on a fast device.
        time.sleep(0.12)

    def _tap_bounds(self, bounds: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = bounds
        self.device.tap((left + right) // 2, (top + bottom) // 2)

    @staticmethod
    def _xml_viewport_size(xml: str) -> tuple[int, int]:
        bounds = [node.bounds for node in parse_ui_xml(xml) if node.bounds]
        width = max((value[2] for value in bounds), default=1080)
        height = max((value[3] for value in bounds), default=2400)
        return width, height

    @staticmethod
    def _sort_control_bounds(
        xml: str, label: str
    ) -> tuple[int, int, int, int] | None:
        """Find a label in the four-item store sort row, not a product card."""
        nodes = parse_ui_xml(xml)
        sort_labels = {"综合", "销量", "上新", "价格"}
        candidates = [
            node.bounds
            for node in nodes
            if node.bounds and (node.text or node.content_desc).strip() == label
        ]
        for candidate in candidates:
            center_y = (candidate[1] + candidate[3]) // 2
            neighbors = sum(
                1
                for node in nodes
                if node.bounds
                and (node.text or node.content_desc).strip() in sort_labels
                and abs(((node.bounds[1] + node.bounds[3]) // 2) - center_y) <= 70
            )
            if neighbors >= 3:
                return candidate
        return None

    @staticmethod
    def _drop_incomplete_edge_products(products: list[ProductRecord]) -> list[ProductRecord]:
        """Ignore clipped viewport-edge cards; collect them fully next screen."""
        kept = list(products)
        while kept and kept[0].image_path is None:
            kept.pop(0)
        while kept and kept[-1].image_path is None:
            kept.pop()
        return kept

    def _open_favorites(self, evidence_dir: Path) -> None:
        """Navigate to 抖音“我 → 收藏 → 商品 → 查看全部” without a store search."""
        # Feed captions and usernames often contain the character “我”; a
        # substring tap can land on the feed instead of the bottom tab.
        if not (self.device.tap_text_exact("我") or self.device.tap_text_exact("我的")):
            raise RuntimeError("未找到抖音底部“我”入口，无法读取我的收藏；已保留现场证据。")
        me_xml_text, me_texts = self._wait_for_ui(
            evidence_dir,
            "02_me",
            lambda current_xml, values: "收藏" in values and "ActionBar$Tab" in current_xml,
            timeout=8.0,
            min_settle=0.25,
        )
        me_xml = evidence_dir / "02_me.xml"
        me_xml.write_text(me_xml_text, encoding="utf-8")
        # 个人页同时存在“收藏”主 tab 和商品卡内的收藏文案；此时只允许点击
        # 个人页主 tab 的坐标，避免 find_text_bounds 命中商品/视频卡中的“收藏”。
        if not self._tap_profile_favorites_tab(me_xml_text):
            raise RuntimeError(
                "已进入抖音个人页，但未找到“收藏”入口；请确认账号已登录且收藏功能可见。"
            )
        favorite_xml_text, favorite_texts = self._wait_for_ui(
            evidence_dir,
            "03_favorites_tab",
            lambda _xml, values: "收藏" in values and "商品" in values,
            timeout=8.0,
            min_settle=0.25,
        )
        favorite_xml = evidence_dir / "03_favorites_tab.xml"
        favorite_xml.write_text(favorite_xml_text, encoding="utf-8")
        if not any("收藏" in text for text in favorite_texts + me_texts):
            raise RuntimeError("未确认进入收藏页，已保留页面 XML 证据。")
        # 商品是收藏页下的分类 tab；点击后会出现商品收藏预览卡。
        if not self.device.tap_text_exact("商品"):
            raise RuntimeError("已进入收藏页，但未找到“商品”分类；已保留 03_favorites_tab.xml。")
        # 商品分类点击后先出现骨架屏；仅检测到分类文字会过早继续，导致
        # OCR 只看到个人页而误报“查看全部不存在”。轮询截图 OCR，直到
        # 自绘的“查看全部”真正出现，同时保留最后一帧 XML/PNG 证据。
        card_xml_text = favorite_xml_text
        card_texts = favorite_texts
        card_xml = evidence_dir / "04_favorites_card.xml"
        card_screenshot = evidence_dir / "04_favorites_card.png"
        view_all_detections: list[OcrDetection] = []
        preview_deadline = time.monotonic() + 12.0
        while time.monotonic() < preview_deadline:
            try:
                card_xml_text = self.device.dump_ui(evidence_dir / ".04_favorites_card_wait.xml").read_text(
                    encoding="utf-8"
                )
                card_texts = extract_texts(card_xml_text)
                self.device.screenshot(card_screenshot)
                view_all_detections = self._product_ocr_detections(
                    card_screenshot, evidence_dir, "04_favorites_card", xml=card_xml_text
                )
                if any("查看全部" in re.sub(r"\s+", "", item.text) for item in view_all_detections):
                    break
            except Exception as exc:  # noqa: BLE001 - retain the last frame and retry while loading
                logger.debug("reading the favorites preview card failed: %s", exc)
            time.sleep(0.45)
        card_xml.write_text(card_xml_text, encoding="utf-8")
        if not any("商品" in text for text in card_texts):
            raise RuntimeError("已点击收藏页的“商品”分类，但未发现商品收藏预览卡；已保留现场证据。")
        # “查看全部”是自绘文字，通常不在 UI XML 中；先尝试 XML，再用本地 OCR 坐标点击。
        if not self.device.tap_text("查看全部") and not self._tap_ocr_text(
            "查看全部", view_all_detections
        ):
            raise RuntimeError(
                "已进入商品收藏预览卡，但未找到“查看全部”入口；"
                "请检查 04_favorites_card.png 及 04_favorites_card_ocr.json。"
            )
        products_xml_text, _products_texts = self._wait_for_ui(
            evidence_dir,
            "05_favorites_products_ready",
            lambda current_xml, values: self._favorite_list_content_ready(
                current_xml, values
            ),
            timeout=8.0,
            min_settle=0.4,
        )
        # Douyin remembers the last scroll offset of 商品收藏.  Tapping the
        # already-selected “全部” filter resets that canvas list to its first
        # row.  “全部” itself is not exposed in the accessibility tree on the
        # current build, but the XML does expose the title bar and viewport,
        # which are enough to resolve the filter row without another OCR pass.
        reset_point = self._favorites_all_filter_point(products_xml_text)
        if reset_point:
            self.device.tap(*reset_point)
            # Resetting “全部” temporarily replaces every row with a skeleton.
            # Do not return to the collector until real collection rows (or an
            # explicit empty/end state) have reappeared.
            self._wait_for_ui(
                evidence_dir,
                "05_favorites_products_reset",
                lambda current_xml, values: self._favorite_list_content_ready(
                    current_xml, values
                ),
                timeout=8.0,
                min_settle=0.25,
            )

    @classmethod
    def _favorite_list_content_ready(cls, xml: str, texts: list[str]) -> bool:
        """Reject the 商品收藏 skeleton while waiting for usable list content."""
        if cls._favorite_card_bounds(xml):
            return True
        joined = " ".join(texts)
        return any(marker in joined for marker in (*_FAVORITE_END_MARKERS, "暂无收藏", "还没有收藏"))

    @classmethod
    def _favorites_all_filter_point(cls, xml: str) -> tuple[int, int] | None:
        """Resolve the canvas-drawn 全部 filter from accessible page geometry."""
        title = find_exact_text_bounds(xml, "商品收藏")
        viewport = cls._xml_viewport_size(xml)
        if not title or not viewport:
            return None
        width, height = viewport
        # Validated on the live page: the filter is centered at 18% width and
        # 3.5% screen height below the title bar.  Clamp the point to the upper
        # quarter so malformed/stale XML cannot tap a product card.
        x = round(width * 0.18)
        y = title[3] + max(60, round(height * 0.035))
        if not (0 < x < width and title[3] < y < round(height * 0.25)):
            return None
        return x, y

    def _tap_profile_favorites_tab(self, xml: str) -> bool:
        """Tap the 收藏 tab on the profile page, not an arbitrary 收藏 label."""
        for node in parse_ui_xml(xml):
            value = node.text or node.content_desc
            if (
                node.bounds
                and value.strip() in {"收藏", "收藏，仅自己可见"}
                and "ActionBar$Tab" in node.class_name
            ):
                left, top, right, bottom = node.bounds
                self.device.tap((left + right) // 2, (top + bottom) // 2)
                return True
        # Older app versions may omit the tab class but retain the exact label.
        return self.device.tap_text("收藏") or self.device.tap_text("我的收藏")

    def _tap_ocr_text(self, target: str, detections: list[OcrDetection]) -> bool:
        target_key = re.sub(r"\s+", "", target)
        for detection in detections:
            text_key = re.sub(r"\s+", "", detection.text or "")
            if target_key and target_key in text_key and detection.confidence >= 0.65:
                left, top, right, bottom = detection.bounds
                self.device.tap((left + right) // 2, (top + bottom) // 2)
                return True
        return False

    @staticmethod
    def _is_favorite_product_list(texts: list[str]) -> bool:
        """Recognize the dedicated list page without requiring a store name.

        The page title and product cards are canvas-rendered on current Douyin builds,
        so UI XML may only expose structural controls (返回/管理/删除).  Keep the
        preview-card guard, then accept either the OCR title or that distinctive list
        structure.
        """
        joined = " ".join(texts)
        has_title = "商品收藏" in joined or "收藏商品" in joined
        if "查看全部" in joined and has_title:
            return False
        if has_title:
            return True
        has_list_controls = "返回" in joined and "管理" in joined
        has_favorite_card_state = any(
            marker in joined
            for marker in ("删除", "商品已失效", "商品失效", "已失效", "你可能还会喜欢")
        )
        return has_list_controls and has_favorite_card_state

    @staticmethod
    def _sort_products(products, sort_mode: ProductSortMode):
        # 排序由抖音列表页完成。本地只按页面出现的顺序采集，任何二次
        # 排序都会破坏“App 显示什么，结果就是什么”的业务原则。
        return products

    @staticmethod
    def _coerce_product_selections(
        product_selections: list[ProductSelection] | None,
        product_titles: list[str] | None,
    ) -> list[ProductSelection]:
        """Normalize new title+price refs and legacy title-only requests."""
        selections: list[ProductSelection] = []
        for selection in product_selections or []:
            if isinstance(selection, ProductSelection):
                item = selection
            elif isinstance(selection, dict):
                item = ProductSelection.model_validate(selection)
            else:
                item = ProductSelection(title=str(selection))
            if item.title.strip():
                selections.append(item.model_copy(update={"title": item.title.strip()}))
        if selections:
            return selections
        return [
            ProductSelection(title=title.strip())
            for title in (product_titles or [])
            if title and title.strip()
        ]

    @staticmethod
    def _selection_matches(product: ProductRecord, selection: ProductSelection) -> bool:
        # UI whitespace/zero-width differences are ignored, but title matching is
        # otherwise exact.  If a saved price exists it is a mandatory second key.
        if Collector._normalize_store_name(product.title) != Collector._normalize_store_name(
            selection.title
        ):
            return False
        return selection.price is None or product.price == selection.price

    @classmethod
    def _requested_products_found(
        cls, products: list[ProductRecord], requested_selections
    ) -> bool:
        return all(
            any(cls._request_matches(product, selection) for product in products)
            for selection in requested_selections
        )

    @classmethod
    def _request_matches(cls, product: ProductRecord, selection) -> bool:
        # Keep the old direct Python/API helper behavior (title substring) for
        # callers that still pass strings. Persisted UI selections use the new
        # ProductSelection branch with exact title+price matching.
        if isinstance(selection, str):
            return cls._normalize_store_name(selection) in cls._normalize_store_name(
                product.title
            )
        return cls._selection_matches(product, selection)

    @classmethod
    def _selection_label(cls, selection: ProductSelection) -> str:
        if selection.price is None:
            return selection.title
        return f"{selection.title}（¥{selection.price:g}）"

    @classmethod
    def _missing_requested_titles(
        cls, products: list[ProductRecord], requested_selections
    ) -> list[str]:
        """Return saved precise refs absent from the captured catalog."""
        missing: list[str] = []
        seen: set[tuple[str, float | None]] = set()
        for selection in requested_selections:
            key = (cls._normalize_store_name(selection.title), selection.price)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            if not any(cls._request_matches(product, selection) for product in products):
                missing.append(
                    selection if isinstance(selection, str) else cls._selection_label(selection)
                )
        return missing

    @classmethod
    def _filter_products(
        cls, products, product_selections, price_min, price_max, result
    ):
        filtered = products
        if product_selections:
            # Resolve each saved ref to the first matching live card.  This is
            # deliberate: if duplicate cards share both title and price, there
            # is no stable UI identity to distinguish them.
            selected_indexes: list[int] = []
            used_indexes: set[int] = set()
            for selection in product_selections:
                index = next(
                    (
                        index
                        for index, product in enumerate(products)
                        if index not in used_indexes and cls._selection_matches(product, selection)
                    ),
                    None,
                )
                if index is not None:
                    selected_indexes.append(index)
                    used_indexes.add(index)
            filtered = [products[index] for index in selected_indexes]
            if not filtered and not result.missing_product_titles:
                result.warnings.append("未找到名称和价格同时匹配的指定商品，请重新读取商品目录。")
        if price_min is not None:
            filtered = [product for product in filtered if product.price is not None and product.price >= price_min]
        if price_max is not None:
            filtered = [product for product in filtered if product.price is not None and product.price <= price_max]
        if (price_min is not None or price_max is not None) and not filtered:
            result.warnings.append("价格筛选后没有可用商品；当前页面可能未读取到价格。")
        return filtered

    def _search(
        self,
        keyword: str,
        evidence_dir: Path,
        *,
        home_xml: str | None = None,
    ) -> None:
        # 优先完整匹配全局“搜索”入口；模糊匹配会把“搜索本店商品”误当成
        # 全局搜索，尤其是在抖音恢复上一次店铺页时会导致输入框始终不存在。
        # The first home-page capture already contains the search button bounds.
        # Reuse them instead of asking UiAutomator2 for another full page source;
        # on Douyin's animated feed that second request can wait for its idle
        # timeout. Fall back to the semantic lookup if the cached hierarchy does
        # not expose the button (for example, an older app build).
        tapped = False
        if home_xml:
            for label in ("搜索", "Search"):
                bounds = find_exact_text_bounds(home_xml, label)
                if not bounds:
                    continue
                left, top, right, bottom = bounds
                self.device.tap((left + right) // 2, (top + bottom) // 2)
                tapped = True
                break
        if not tapped:
            tapped = self.device.tap_text_exact("搜索") or self.device.tap_text_exact("Search")
        # 当前版本有时把搜索图标做成无障碍树中的空 ImageView。首页右上角
        # 搜索图标位置稳定，作为最后的 UI fallback，但绝不点击店铺页中的
        # “搜索本店商品”文本。
        if not tapped:
            info = self.device.info()
            width = info.screen_width or 1080
            height = info.screen_height or 2400
            self.device.tap(int(width * 0.93), min(int(height * 0.10), 280))
            tapped = True
        if not tapped:
            raise RuntimeError("未找到搜索入口；请人工打开抖音搜索页后重新运行。")
        try:
            self.device.type_text(keyword)
            self.device.press_enter()
        except Exception as exc:
            (evidence_dir / "search_input_error.txt").write_text(str(exc), encoding="utf-8")
            if self.device.name == "adb" and any(ord(char) > 127 for char in keyword):
                raise LoginRequired(
                    "ADB 后端不支持直接输入中文关键词，请切换 Appium 后端，"
                    "或用 scrcpy 手动输入关键词后重新运行。"
                ) from exc
            if isinstance(exc, DeviceError):
                raise
            raise DeviceError(f"抖音搜索输入失败：{exc}") from exc

    def _prepare_global_search(self, evidence_dir: Path) -> None:
        """Return to Douyin's home route before opening global search.

        A precise-catalog read ends on the store's product page. Android may
        restore that route when the next task starts, and the top-right icon
        there opens ``搜索本店商品`` rather than global search. Back out only
        when the live hierarchy confirms a store/search-results page; never
        blindly press Back on the home page.
        """
        for attempt in range(4):
            ui_path = self.device.dump_ui(evidence_dir / f"01_ready_{attempt + 1:02d}.xml")
            xml = ui_path.read_text(encoding="utf-8")
            texts = extract_texts(xml)
            self._guard_page(texts)
            if self._is_home_page(texts):
                return
            if self._is_store_page(texts) or self._has_search_result_tabs(texts):
                self.device.back()
                time.sleep(0.35)
                continue
            # The first hierarchy after app activation can still be empty;
            # allow one settling round before falling through to search's
            # coordinate fallback.
            time.sleep(0.25)

    def _wait_for_ui(
        self,
        evidence_dir: Path,
        stem: str,
        predicate,
        *,
        timeout: float = 8.0,
        min_settle: float = 0.25,
        poll_interval: float = 0.25,
    ) -> tuple[str, list[str]]:
        """Poll a cheap UI predicate instead of sleeping a fixed page interval.

        There is no reliable document-ready event for a native Canvas page.  A
        bounded UI-tree poll is therefore the simplest reliable compromise:
        it reacts early when the expected control appears, but still has a
        timeout and leaves the final hierarchy in the evidence directory.
        """
        deadline = time.monotonic() + timeout
        earliest = time.monotonic() + min_settle
        latest_xml = ""
        latest_texts: list[str] = []
        wait_path = evidence_dir / f".{stem}_wait.xml"
        while True:
            try:
                wait_path = self.device.dump_ui(wait_path)
                latest_xml = wait_path.read_text(encoding="utf-8")
                latest_texts = extract_texts(latest_xml)
            except Exception:  # noqa: BLE001 - retain the last usable UI state
                if not latest_xml:
                    latest_texts = []
            if latest_xml and time.monotonic() >= earliest and predicate(latest_xml, latest_texts):
                return latest_xml, latest_texts
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest_xml, latest_texts
            time.sleep(min(poll_interval, remaining))

    def _wait_for_store_result(
        self,
        evidence_dir: Path,
        target: str | None,
        *,
        timeout: float = 25.0,
        poll_interval: float = 0.25,
        ocr_interval: float = 0.8,
        screenshot_path: Path | None = None,
    ) -> tuple[str, list[str], list[OcrDetection]]:
        """Wait for a real store card, including Canvas-only cards.

        The selected ``店铺`` tab is exposed by UiAutomator quickly, while the
        card body is usually painted on a Canvas and absent from the hierarchy.
        Polling only ``page_source`` therefore either races the blank spinner or
        waits the complete timeout.  A bounded screenshot/OCR probe is used only
        on this transition; once the exact card name is visible the normal
        extraction path reuses those detections instead of running OCR again.
        """
        deadline = time.monotonic() + timeout
        earliest = time.monotonic() + 0.25
        next_ocr = earliest
        latest_xml = ""
        latest_texts: list[str] = []
        latest_detections: list[OcrDetection] = []
        wait_xml = evidence_dir / ".03_store_search_wait.xml"
        wait_png = screenshot_path or evidence_dir / ".03_store_search_wait.png"
        recognize_boxes = getattr(self.ocr, "recognize_boxes", None)
        # The Web default keeps the explicit OCR provider at ``none`` so
        # setup remains lightweight, but a live Douyin store card still needs
        # local OCR.  Lazily prepare the same RapidOCR fallback used by final
        # extraction, allowing the wait loop to detect the Canvas card instead
        # of burning the entire 25-second timeout first.
        if not callable(recognize_boxes) and self.device.is_live:
            if not self._auto_ocr_attempted:
                self._auto_ocr_attempted = True
                try:
                    from wen.extract.ocr import RapidOcrProvider

                    self._auto_ocr_provider = RapidOcrProvider()
                except Exception as exc:  # noqa: BLE001 - final extraction keeps the diagnostic
                    self._auto_ocr_error = str(exc)
            recognize_boxes = getattr(self._auto_ocr_provider, "recognize_boxes", None)
        can_probe_ocr = callable(recognize_boxes)
        while True:
            try:
                wait_xml = self.device.dump_ui(wait_xml)
                latest_xml = wait_xml.read_text(encoding="utf-8")
                latest_texts = extract_texts(latest_xml)
            except Exception as exc:  # noqa: BLE001 - retain the last usable hierarchy
                logger.debug("reading the store results hierarchy failed: %s", exc)
            now = time.monotonic()
            if now >= earliest and self._xml_has_selected_label(latest_xml, "店铺"):
                if self._xml_has_store_candidate(latest_xml, target or ""):
                    return latest_xml, latest_texts, latest_detections
                if can_probe_ocr and now >= next_ocr:
                    try:
                        self.device.screenshot(wait_png)
                        if self.ocr.name != "none":
                            latest_detections = self._ocr_detections(wait_png)
                        else:
                            latest_detections = list(recognize_boxes(wait_png))
                        candidates = self._store_candidates(latest_xml, latest_detections)
                        if not target or self._select_store_candidate(candidates, target):
                            return latest_xml, latest_texts, latest_detections
                    except Exception as exc:  # noqa: BLE001 - final extraction reports OCR errors
                        logger.debug("probing store candidates with OCR failed: %s", exc)
                    next_ocr = now + ocr_interval
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest_xml, latest_texts, latest_detections
            time.sleep(min(poll_interval, remaining))

    @staticmethod
    def _xml_has_selected_label(xml: str, label: str) -> bool:
        escaped = re.escape(label)
        return bool(
            re.search(
                rf'<[^>]+(?:text|content-desc)="{escaped}"[^>]*selected="true"',
                xml,
            )
            or re.search(
                rf'<[^>]+selected="true"[^>]*(?:text|content-desc)="{escaped}"',
                xml,
            )
        )

    @classmethod
    def _xml_has_store_candidate(cls, xml: str, target: str) -> bool:
        """Return true only for a store-card name, not the search EditText.

        The search result hierarchy repeats the keyword in the top input.  A
        plain ``target in values`` check therefore reports success while the
        store tab is still showing a blank loading spinner.  Candidate cards
        are below the tab strip and are not EditText nodes.
        """
        normalized_target = cls._normalize_store_name(target)
        for node in parse_ui_xml(xml):
            value = cls._normalize_store_name(node.text or node.content_desc)
            if value != normalized_target:
                continue
            if "EditText" in node.class_name:
                continue
            if node.bounds and node.bounds[1] > 430:
                return True
        return False

    @staticmethod
    def _is_home_page(texts: list[str]) -> bool:
        return "首页" in texts and bool({"推荐", "商城", "朋友", "消息"}.intersection(texts))

    @classmethod
    def _store_candidates(
        cls,
        xml: str,
        ocr_detections: list[OcrDetection] | None = None,
    ) -> list[tuple[str, tuple[int, int, int, int]]]:
        """合并 UI XML 和 OCR 候选；自绘店铺卡通常只会出现在 OCR 通道。"""
        candidates: dict[str, tuple[int, int, int, int]] = {}
        input_bounds = [
            node.bounds
            for node in parse_ui_xml(xml)
            if node.class_name.endswith("EditText") and node.bounds
        ]
        for node in parse_ui_xml(xml):
            if node.class_name.endswith("EditText") or not node.bounds:
                continue
            raw = node.text or node.content_desc
            name = cls._clean_candidate_name(raw)
            if cls._is_store_candidate(name):
                candidates[name] = cls._prefer_bounds(candidates.get(name), node.bounds)
        for detection in ocr_detections or []:
            name = cls._clean_candidate_name(detection.text)
            # OCR 往往会重复读到顶部搜索框中的关键词。该区域已经由 UI XML
            # 识别为 EditText，不应被当成店铺卡候选。
            if any(cls._bounds_overlap(detection.bounds, bounds) for bounds in input_bounds):
                continue
            if cls._is_store_candidate(name) and detection.confidence >= 0.75:
                candidates[name] = cls._prefer_bounds(candidates.get(name), detection.bounds)
        return list(candidates.items())

    @staticmethod
    def _clean_candidate_name(raw: str) -> str:
        return re.sub(r"[，,、|·]+\s*按钮$", "", raw or "").strip()

    @staticmethod
    def _is_store_candidate(name: str) -> bool:
        return bool(
            4 <= len(name) <= 80
            and "店" in name
            and name not in {"店铺", "店内商品", "进店", "进店逛逛"}
            and "进店" not in name
            and "搜索本店" not in name
            and "榜" not in name
        )

    @staticmethod
    def _bounds_overlap(
        first: tuple[int, int, int, int], second: tuple[int, int, int, int]
    ) -> bool:
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[2], second[2])
        bottom = min(first[3], second[3])
        return right > left and bottom > top

    @classmethod
    def _prefer_bounds(
        cls,
        previous: tuple[int, int, int, int] | None,
        current: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        return current if previous is None or cls._area(current) < cls._area(previous) else previous

    @staticmethod
    def _select_store_candidate(
        candidates: list[tuple[str, tuple[int, int, int, int]]], target: str
    ) -> tuple[int, int, int, int] | None:
        target_normalized = Collector._normalize_store_name(target)
        for name, bounds in candidates:
            if Collector._normalize_store_name(name) == target_normalized:
                return bounds
        return None

    @classmethod
    def _store_entry_bounds(
        cls,
        name_bounds: tuple[int, int, int, int],
        ocr_detections: list[OcrDetection],
        xml: str,
    ) -> tuple[int, int, int, int] | None:
        """Find the nearest ``进店`` action for a verified store card."""
        name_center_y = (name_bounds[1] + name_bounds[3]) // 2
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for detection in ocr_detections:
            if "进店" not in re.sub(r"\s+", "", detection.text or ""):
                continue
            center_y = (detection.bounds[1] + detection.bounds[3]) // 2
            distance = abs(center_y - name_center_y)
            if distance <= 420 and detection.confidence >= 0.75:
                candidates.append((distance, detection.bounds))
        if candidates:
            return min(candidates, key=lambda item: item[0])[1]
        # Some builds expose the action in XML even when the card name is OCR.
        for node in parse_ui_xml(xml):
            value = re.sub(r"\s+", "", node.text or node.content_desc)
            if "进店" not in value or not node.bounds:
                continue
            center_y = (node.bounds[1] + node.bounds[3]) // 2
            if abs(center_y - name_center_y) <= 420:
                return node.bounds
        return None

    @staticmethod
    def _normalize_store_name(value: str) -> str:
        return re.sub(r"[\s\u200b\ufeff]+", "", value or "").strip()

    @staticmethod
    def _area(bounds: tuple[int, int, int, int]) -> int:
        left, top, right, bottom = bounds
        return max(1, right - left) * max(1, bottom - top)

    def _ocr_detections(
        self,
        screenshot: Path,
        *,
        xml: str | None = None,
        evidence_dir: Path | None = None,
        evidence_stem: str | None = None,
    ) -> list[OcrDetection]:
        recognize_boxes = getattr(self.ocr, "recognize_boxes", None)
        if not callable(recognize_boxes):
            return []
        input_path, offset = self._prepare_ocr_input(
            screenshot, xml=xml, evidence_dir=evidence_dir, evidence_stem=evidence_stem
        )
        detections = list(recognize_boxes(input_path))
        return [self._offset_detection(item, offset) for item in detections]

    def _plain_ocr_texts_if_needed(self, screenshot: Path) -> list[str]:
        """Use plain full-screen OCR only for providers without box support."""
        if self.ocr.name == "none":
            return []
        if callable(getattr(self.ocr, "recognize_boxes", None)):
            return []
        return self.ocr.recognize(screenshot)

    @staticmethod
    def _offset_detection(
        detection: OcrDetection, offset: tuple[int, int]
    ) -> OcrDetection:
        if offset == (0, 0):
            return detection
        left, top, right, bottom = detection.bounds
        ox, oy = offset
        return OcrDetection(
            detection.text,
            (left + ox, top + oy, right + ox, bottom + oy),
            detection.confidence,
        )

    def _prepare_ocr_input(
        self,
        screenshot: Path,
        *,
        xml: str | None,
        evidence_dir: Path | None,
        evidence_stem: str | None,
    ) -> tuple[Path, tuple[int, int]]:
        """Crop one shared product-information ROI before OCR.

        Titles and prices are normally available in UI XML.  The ROI therefore
        covers the right-hand price/sales bands for all visible cards in one
        OCR call.  If XML has no usable price anchors, the original screenshot
        is retained for safety rather than guessing coordinates.
        """
        if not xml or evidence_dir is None or not screenshot.exists():
            return screenshot, (0, 0)
        region = self._product_ocr_region(xml, screenshot)
        if region is None:
            return screenshot, (0, 0)
        try:
            from PIL import Image

            left, top, _right, _bottom = region
            with Image.open(screenshot) as source:
                crop_path = evidence_dir / f"{evidence_stem or 'product'}_ocr_roi.png"
                source.crop(region).save(crop_path, format="PNG", optimize=True)
            return crop_path, (left, top)
        except Exception:  # noqa: BLE001 - full-screen OCR is the safe fallback
            return screenshot, (0, 0)

    @staticmethod
    def _product_ocr_region(
        xml: str, screenshot: Path
    ) -> tuple[int, int, int, int] | None:
        """Find a conservative shared ROI from UI price nodes.

        The calculation follows the current screenshot/XML scale, so it is not
        tied to the 1080/1279-pixel device used during development.
        """
        try:
            from PIL import Image

            with Image.open(screenshot) as source:
                image_width, image_height = source.size
        except Exception:  # noqa: BLE001 - caller will use the full image
            return None
        nodes = parse_ui_xml(xml)
        price_bounds = []
        ui_width = 0
        ui_height = 0
        for node in nodes:
            if not node.bounds:
                continue
            left, top, right, bottom = node.bounds
            ui_width = max(ui_width, right)
            ui_height = max(ui_height, bottom)
            value = node.text or node.content_desc or ""
            if parse_price(value) is not None and ("¥" in value or "￥" in value):
                price_bounds.append(node.bounds)
        if not price_bounds or not ui_width or not ui_height:
            return None
        scale_x = image_width / ui_width
        scale_y = image_height / ui_height
        left = max(0, round((min(item[0] for item in price_bounds) - 120) * scale_x))
        top = max(0, round((min(item[1] for item in price_bounds) - 70) * scale_y))
        right = min(image_width, round((max(item[2] for item in price_bounds) + 350) * scale_x))
        # Include the lower edge of the viewport.  A partially visible last
        # card can expose its sales line below the last UI price node; clipping
        # the ROI at that price was a direct cause of missed final-card data.
        bottom = min(image_height, round((max(item[3] for item in price_bounds) + 650) * scale_y))
        if right - left < 120 or bottom - top < 80:
            return None
        # A nearly full-screen crop costs the same while losing no noise; use
        # the original image in that case.
        if (right - left) * (bottom - top) >= image_width * image_height * 0.82:
            return None
        return left, top, right, bottom

    def _product_ocr_detections(
        self,
        screenshot: Path,
        evidence_dir: Path,
        evidence_stem: str,
        *,
        xml: str | None = None,
    ) -> list[OcrDetection]:
        """Read screenshot-only product labels, lazily using local RapidOCR when needed.

        Douyin renders labels such as ``已售 7万+`` outside the accessibility tree.  The
        configured OCR provider remains authoritative; when it is disabled for a real
        device, one local RapidOCR instance is loaded on demand and reused for every
        product-list screenshot.  No network or hosted model is involved.
        """
        detections = self._ocr_detections(
            screenshot,
            xml=xml,
            evidence_dir=evidence_dir,
            evidence_stem=evidence_stem,
        )
        if detections or self.ocr.name != "none" or not self.device.is_live:
            return detections
        if not self._auto_ocr_attempted:
            self._auto_ocr_attempted = True
            try:
                from wen.extract.ocr import RapidOcrProvider

                self._auto_ocr_provider = RapidOcrProvider()
            except Exception as exc:  # noqa: BLE001 - keep UI/XML collection usable
                self._auto_ocr_error = str(exc)
        if self._auto_ocr_provider is None:
            return []
        try:
            recognize_boxes = self._auto_ocr_provider.recognize_boxes  # type: ignore[attr-defined]
            input_path, offset = self._prepare_ocr_input(
                screenshot,
                xml=xml,
                evidence_dir=evidence_dir,
                evidence_stem=evidence_stem,
            )
            detections = [
                self._offset_detection(item, offset)
                for item in recognize_boxes(input_path)
            ]
            (evidence_dir / f"{evidence_stem}_ocr.json").write_text(
                json.dumps(
                    [
                        {
                            "text": detection.text,
                            "bounds": detection.bounds,
                            "confidence": detection.confidence,
                        }
                        for detection in detections
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return detections
        except Exception as exc:  # noqa: BLE001 - retain evidence and continue without OCR
            self._auto_ocr_error = str(exc)
            (evidence_dir / f"{evidence_stem}_ocr_error.txt").write_text(
                str(exc), encoding="utf-8"
            )
            return []

    def _detect_store_candidates(
        self,
        xml: str,
        screenshot: Path,
        evidence_dir: Path,
        *,
        ocr_detections: list[OcrDetection] | None = None,
    ) -> list[tuple[str, tuple[int, int, int, int]]]:
        """读取搜索结果中的店铺卡；没有 UI 文本时按需启用本地 OCR。"""
        # The store-result wait may already have recognized this exact frame.
        # Reuse those boxes to avoid a second full-screen OCR pass (often the
        # most expensive operation in this transition).
        detections = (
            self._ocr_detections(screenshot) if ocr_detections is None else ocr_detections
        )
        candidates = self._store_candidates(xml, detections)
        if candidates:
            return candidates

        # When a configured OCR provider was already used, retrying the same
        # frame cannot improve the result.  Keep the diagnostic immediate;
        # the RapidOCR fallback below remains available for the ``none`` mode.
        if ocr_detections is not None and self.ocr.name != "none":
            raise RuntimeError(
                "已进入抖音搜索的“店铺”标签，但 OCR 未识别到店铺卡名称；"
                "请查看 03_store_search.png/03_store_ocr.json，确认页面是否仍在加载或发生了风控。"
            )

        # 抖音店铺卡通常是自绘内容，UIAutomator 只会返回空容器。为了让
        # Web 控制台开箱即用，未显式配置 OCR 时在这里懒加载 RapidOCR；
        # 只有搜索店铺卡需要它，不会让离线测试设备或普通 UI 流程承担 OCR 成本。
        try:
            from wen.extract.ocr import RapidOcrProvider

            fallback = RapidOcrProvider()
            fallback_detections = list(fallback.recognize_boxes(screenshot))
        except Exception as exc:
            (evidence_dir / "03_store_ocr_error.txt").write_text(str(exc), encoding="utf-8")
            raise RuntimeError(
                "已进入抖音搜索的“店铺”标签，但店铺卡是自绘内容，UI 层没有暴露店铺名称；"
                "本地 OCR 初始化或识别失败。请执行“uv sync --extra ocr”，"
                "或设置 WEN_OCR_PROVIDER=rapidocr 后重试。"
                f" OCR 原因：{exc}"
            ) from exc

        (evidence_dir / "03_store_ocr.json").write_text(
            json.dumps(
                [
                    {
                        "text": detection.text,
                        "bounds": detection.bounds,
                        "confidence": detection.confidence,
                    }
                    for detection in fallback_detections
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        candidates = self._store_candidates(xml, fallback_detections)
        if not candidates:
            raise RuntimeError(
                "已进入抖音搜索的“店铺”标签，但 OCR 也没有识别到店铺卡名称；"
                "请查看证据目录中的 03_store_search.png/03_store_ocr.json，"
                "确认页面是否仍在加载或发生了风控。"
            )
        return candidates

    @staticmethod
    def _is_store_page(texts: list[str]) -> bool:
        joined = " ".join(texts)
        return "搜索本店商品" in joined and "商品" in texts and "分类" in texts

    @staticmethod
    def _has_search_result_tabs(texts: list[str]) -> bool:
        """判断当前是否已经是搜索结果页，而不是搜索输入/建议页。"""
        tab_names = {"综合", "商品", "用户", "直播", "店铺", "视频"}
        return len(tab_names.intersection(texts)) >= 3

    @staticmethod
    def _login_required(texts: list[str]) -> bool:
        joined = " ".join(texts)
        return any(marker in joined for marker in _LOGIN_MARKERS)

    def _dismiss_common_overlay(self, texts: list[str]) -> bool:
        joined = " ".join(texts)
        if "未成年人模式" not in joined and "青少年模式" not in joined:
            return False
        # 关闭只是关闭当前提示；不自动选择“开启”或修改年龄/身份设置。
        dismissed = False
        for _ in range(3):
            if self.device.tap_text("关闭"):
                dismissed = True
                time.sleep(0.5)
                continue
            break
        if dismissed:
            return True
        return self.device.tap_text("不再提醒")

    @staticmethod
    def _guard_page(texts: list[str]) -> None:
        joined = " ".join(texts)
        # 验证码登录本身是登录页面，不应一律作为风控；只有明确验证/频繁访问文案才暂停。
        risk = ("访问频繁", "请完成验证", "安全验证", "滑块验证", "Application has no permissions")
        if any(marker in joined for marker in risk):
            raise RiskControlDetected("检测到抖音登录/访问验证页面，任务已暂停，请人工确认后再继续。")

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value)[:50] or "task"
