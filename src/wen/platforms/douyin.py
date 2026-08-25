from __future__ import annotations

import re

from wen.extract.ocr import OcrDetection
from wen.extract.ui_xml import UiNode, extract_texts, parse_ui_xml
from wen.models import FieldValue, ProductRecord, StoreRecord

_COUNT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[万千百亿kKmMwW]?)\s*\+?")
_PRICE_RE = re.compile(r"(?:¥|￥|价格)\s*(\d+(?:\.\d{1,2})?)")
_PLAIN_PRICE_RE = re.compile(r"^\s*(\d+(?:\.\d{1,2})?)\s*$")
_SALES_RE = re.compile(r"(?:已售|销量|销售|售出)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万千百亿kKmMwW]?\+?)")
_FOLLOWER_RE = re.compile(r"(?:粉丝|关注者)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万千百亿kKmM]?\+?)")
_REVIEW_RE = re.compile(r"(?:评价|评论)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万千百亿kKmMwW]?\+?)")
_RATING_RE = re.compile(r"(?:评分|好评率)\s*[:：]?\s*(\d+(?:\.\d+)?)")
_UI_BADGE_PREFIX_RE = re.compile(r"^[iv]\s*(?=(?:YAYA|鸭鸭|[\u4e00-\u9fff]))", re.IGNORECASE)


def parse_count(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    match = _COUNT_RE.search(cleaned)
    if not match:
        return None
    number = float(match.group("number"))
    unit = match.group("unit").lower()
    multiplier = {"": 1, "k": 1_000, "千": 1_000, "m": 1_000_000, "万": 10_000, "百": 100, "亿": 100_000_000, "w": 10_000}.get(unit, 1)
    return int(number * multiplier)


def parse_price(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    match = _PRICE_RE.search(cleaned) or _PLAIN_PRICE_RE.fullmatch(cleaned)
    return float(match.group(1)) if match else None


def _looks_like_product(text: str) -> bool:
    text = _clean_text(text)
    if len(text) < 4 or len(text) > 120:
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", text) or re.fullmatch(r"满\d+减\d+", text):
        return False
    excluded = (
        "登录",
        "协议",
        "隐私",
        "搜索",
        "首页",
        "推荐",
        "关注",
        "粉丝",
        "已售",
        "销量",
        "销售",
        "售出",
        "旗舰店",
        "按钮",
        "进店",
        "直播中",
        "蜘蛛侠",
        "官方正品",
        "筛选",
        "切换图标",
        "发布了内容",
        "好评率",
        # 商品卡标题上方/下方的营销标签也会出现在 UI 文本树中。
        # 它们不是商品名，不能参与价格、销量和图片的卡片关联。
        "达人说",
        "直播间同价",
        "短视频带过",
        "店铺加购",
        "尺码合适",
        "物美价廉",
        "防钻绒好",
        "同款好评",
        "人逛过",
        "回购",
        "收藏",
        "正在看",
        "优惠",
        "优惠券",
        "已享",
        "加购",
        "券后价",
        "现价",
        "立减",
        "满减",
        "运费险",
        "入会",
        "再减",
        "到手",
        "会员",
        "领券",
    )
    return (
        not any(word in text for word in excluded)
        and not bool(_PRICE_RE.search(text))
        and not bool(re.search(r"\d+\s*元", text))
    )


def _clean_text(text: str) -> str:
    """去掉抖音商品卡常见的零宽字符，保留原始值由证据 XML 保存。"""
    return re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text or "").strip()


def _clean_product_title(text: str) -> str:
    """Remove canvas badge markers exposed as a leading ``i`` or ``v``.

    On the current Douyin list UI, cards with labels such as ``新品·加厚羽绒服``
    sometimes expose that label's icon as ``i`` or ``v`` in the same TextView as
    the title. Only strip those observed markers immediately before Chinese text;
    real names such as ``iPhone`` and ``vivo`` remain unchanged.
    """
    return _UI_BADGE_PREFIX_RE.sub("", _clean_text(text))


class DouyinExtractor:
    platform = "douyin"

    def extract_product_detail(
        self,
        xml: str,
        product_id: str,
        ocr_detections: list[OcrDetection] | None = None,
    ) -> ProductRecord:
        """Extract one product from its detail page without using list-card logic.

        The current Douyin detail page exposes title, sales and the purchase
        buttons through accessibility XML.  Its large body price is custom
        drawn, so the matching bottom purchase button is preferred; OCR is a
        bounded fallback only when XML has no actionable price.
        """
        nodes = parse_ui_xml(xml)
        values = [_clean_text(node.text or node.content_desc) for node in nodes]
        image_bounds = [
            node.bounds
            for node in nodes
            if node.bounds and (node.text or node.content_desc).strip() in {"图片1", "商品图片1"}
        ]
        image_bottom = max((bounds[3] for bounds in image_bounds), default=0)
        viewport_bottom = max((node.bounds[3] for node in nodes if node.bounds), default=10**9)
        title_candidates: list[tuple[str, tuple[int, int, int, int]]] = []
        for node in nodes:
            value = _clean_product_title(node.text or node.content_desc)
            if not node.bounds or not _looks_like_product(value):
                continue
            left, top, right, bottom = node.bounds
            if top < image_bottom or bottom >= viewport_bottom * 0.92:
                continue
            if right - left < 500:
                continue
            title_candidates.append((value, node.bounds))
        if not title_candidates:
            raise ValueError(f"商品 {product_id} 的详情页已打开，但没有读取到商品名称。")
        title, _title_bounds = max(
            title_candidates,
            key=lambda item: ((item[1][2] - item[1][0]), len(item[0]), item[1][1]),
        )

        price, raw_price, price_method = self._detail_purchase_price(nodes, ocr_detections or [])
        sales_raw: str | None = None
        for value in values:
            match = _SALES_RE.search(value)
            if match:
                sales_raw = match.group(1).strip()
                break
        review_count = next(
            (parse_count(match.group(1)) for value in values if (match := _REVIEW_RE.search(value))),
            None,
        )
        rating = next(
            (float(match.group(1)) for value in values if (match := _RATING_RE.search(value))),
            None,
        )
        fields: list[FieldValue] = []
        if price is not None:
            fields.append(
                FieldValue(
                    key="price",
                    value=price,
                    raw_value=raw_price,
                    method=price_method,
                    confidence=0.96 if price_method == "ui" else 0.82,
                )
            )
        if sales_raw is not None:
            fields.append(
                FieldValue(
                    key="displayed_sales",
                    value=parse_count(sales_raw),
                    raw_value=sales_raw,
                    method="ui",
                    confidence=0.96,
                )
            )
        # Preserve alternative purchase prices rather than mislabelling them
        # as an original/list price.  For example, “App 专享价” can be lower
        # than “券后价” and is not a strikethrough original price.
        for label in ("专享价", "到手价", "活动价"):
            matched = self._price_for_label(nodes, label)
            if matched and matched[0] != price:
                fields.append(
                    FieldValue(
                        key={"专享价": "app_exclusive_price", "到手价": "final_price", "活动价": "campaign_price"}[label],
                        value=matched[0],
                        raw_value=f"{label} {matched[1]}",
                        method="ui",
                        confidence=0.94,
                    )
                )
        return ProductRecord(
            title=title,
            price=price,
            displayed_sales=parse_count(sales_raw),
            displayed_sales_raw=sales_raw,
            rating=rating,
            review_count=review_count,
            product_id=product_id,
            source_url=(
                "snssdk1128://ec_goods_detail?"
                f"product_id={product_id}&promotion_id={product_id}"
            ),
            fields=fields,
        )

    @staticmethod
    def _price_for_label(
        nodes: list[UiNode], label: str
    ) -> tuple[float, str] | None:
        labels = [
            node for node in nodes
            if node.bounds and _clean_text(node.text or node.content_desc) == label
        ]
        priced = [
            (node, parse_price(_clean_text(node.text or node.content_desc)))
            for node in nodes
            if node.bounds and ("¥" in (node.text or node.content_desc) or "￥" in (node.text or node.content_desc))
        ]
        candidates: list[tuple[int, float, str]] = []
        for label_node in labels:
            _ll, lt, lr, lb = label_node.bounds
            label_center_y = (lt + lb) // 2
            for price_node, price in priced:
                if price is None:
                    continue
                pl, pt, _pr, pb = price_node.bounds
                distance_y = abs(((pt + pb) // 2) - label_center_y)
                if pl < lr - 20 or distance_y > 90:
                    continue
                raw = _clean_text(price_node.text or price_node.content_desc)
                candidates.append((distance_y + max(0, pl - lr) // 8, price, raw))
        if not candidates:
            return None
        _score, price, raw = min(candidates, key=lambda item: item[0])
        return price, raw

    @staticmethod
    def _purchase_action_price(nodes: list[UiNode]) -> tuple[float, str] | None:
        """Read the currency value rendered inside a purchase action.

        The current product-detail template exposes the coupon/current price as
        a currency node immediately above ``立即购买`` while the nearby App-only
        price is separately labelled ``专享价``.  Bind by geometry so the two
        business meanings cannot be mixed.
        """
        actions = [
            node
            for node in nodes
            if node.bounds
            and any(
                label in _clean_text(node.text or node.content_desc)
                for label in (
                    "立即购买", "去购买", "马上抢", "立即抢购", "现在下单", "去下单",
                )
            )
        ]
        prices = [
            (node, parse_price(_clean_text(node.text or node.content_desc)))
            for node in nodes
            if node.bounds
            and ("¥" in (node.text or node.content_desc) or "￥" in (node.text or node.content_desc))
        ]
        candidates: list[tuple[int, float, str, str]] = []
        for action in actions:
            al, at, ar, ab = action.bounds
            action_label = _clean_text(action.text or action.content_desc)
            action_center_x = (al + ar) // 2
            action_center_y = (at + ab) // 2
            for price_node, price in prices:
                if price is None:
                    continue
                pl, pt, pr, pb = price_node.bounds
                price_center_x = (pl + pr) // 2
                price_center_y = (pt + pb) // 2
                if not (al - 90 <= price_center_x <= ar + 90):
                    continue
                if abs(price_center_y - action_center_y) > 170:
                    continue
                raw = _clean_text(price_node.text or price_node.content_desc)
                score = abs(price_center_x - action_center_x) + abs(price_center_y - action_center_y)
                candidates.append((score, price, raw, action_label))
        if not candidates:
            return None
        _score, price, raw, action_label = min(candidates, key=lambda item: item[0])
        return price, f"{action_label} {raw}"

    @classmethod
    def _detail_purchase_price(
        cls,
        nodes: list[UiNode],
        detections: list[OcrDetection],
    ) -> tuple[float | None, str | None, str]:
        for label in ("券后价", "到手价", "现价", "活动价", "价格"):
            matched = cls._price_for_label(nodes, label)
            if matched:
                return matched[0], f"{label} {matched[1]}", "ui"

        purchase_price = cls._purchase_action_price(nodes)
        if purchase_price:
            return purchase_price[0], purchase_price[1], "ui"

        label_bounds = [
            node.bounds
            for node in nodes
            if node.bounds
            and _clean_text(node.text or node.content_desc) in {"券后价", "到手价", "现价", "活动价"}
        ]
        exclusive_label_bounds = [
            node.bounds
            for node in nodes
            if node.bounds
            and _clean_text(node.text or node.content_desc) == "专享价"
        ]
        for detection in detections:
            # A price must carry a currency sign.  Bare numbers can be battery
            # percentage, time, sales, size or other unrelated page content.
            if "¥" not in detection.text and "￥" not in detection.text:
                continue
            if "专享" in detection.text:
                continue
            price = parse_price(detection.text)
            if price is None:
                continue
            center_y = (detection.bounds[1] + detection.bounds[3]) // 2
            if label_bounds and not any(
                abs(center_y - ((bounds[1] + bounds[3]) // 2)) <= 140
                for bounds in label_bounds
            ):
                continue
            dl, _dt, _dr, _db = detection.bounds
            if any(
                abs(center_y - ((bounds[1] + bounds[3]) // 2)) <= 110
                and dl >= bounds[0] - 30
                and dl <= bounds[2] + 320
                for bounds in exclusive_label_bounds
            ):
                continue
            return price, detection.text, "ocr"
        return None, None, "ui"

    def extract(
        self,
        xml: str,
        keyword: str,
        ocr_texts: list[str] | None = None,
        ocr_detections: list[OcrDetection] | None = None,
    ) -> tuple[StoreRecord, list[ProductRecord]]:
        ui_texts = [_clean_text(text) for text in extract_texts(xml) if _clean_text(text)]
        texts = list(ui_texts)
        # 有坐标的 OCR 用于把自绘销量绑定到 UI 树中的完整商品标题；无坐标 OCR 才合并到
        # 普通文本解析，避免“券后价/人逛过”等自绘标签被误判成商品标题。
        if not ocr_detections:
            for text in ocr_texts or []:
                cleaned = _clean_text(text)
                if cleaned and cleaned not in texts:
                    texts.append(cleaned)
        store_name = self._store_name(texts, keyword)
        store_fields: list[FieldValue] = []
        followers = self._first_count(texts, _FOLLOWER_RE)
        if followers is not None:
            store_fields.append(FieldValue(key="followers", value=followers, method="ui"))
        product_count = self._labeled_count(texts, ("商品", "宝贝"))
        if product_count is not None:
            store_fields.append(FieldValue(key="product_count", value=product_count, method="ui"))
        store = StoreRecord(
            keyword=keyword,
            name=store_name,
            followers=followers,
            product_count=product_count,
            fields=store_fields,
        )
        products = self._products(texts, parse_ui_xml(xml), ocr_detections)
        return store, products

    @staticmethod
    def _store_name(texts: list[str], keyword: str) -> str | None:
        exact = [text for text in texts if keyword and keyword in text]
        if exact:
            return max(exact, key=len)
        candidates = [text for text in texts if "旗舰店" in text or "专卖店" in text]
        return max(candidates, key=len) if candidates else None

    @staticmethod
    def _first_count(texts: list[str], pattern: re.Pattern[str]) -> int | None:
        for text in texts:
            match = pattern.search(text)
            if match:
                return parse_count(match.group(1))
        return None

    @staticmethod
    def _labeled_count(texts: list[str], labels: tuple[str, ...]) -> int | None:
        for text in texts:
            if any(label in text for label in labels):
                count = parse_count(text)
                if count is not None:
                    return count
        return None

    @staticmethod
    def _title_key(value: str) -> str:
        """Normalize a title only for joining XML/OCR observations."""
        return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", _clean_text(value))

    @staticmethod
    def _title_nodes(nodes: list[UiNode]) -> list[tuple[str, tuple[int, int, int, int]]]:
        """Return product titles and bounds in top-to-bottom order.

        Same-title cards are kept when their screen bounds differ. A shop can expose
        two cards with the same visible title but different prices/promotions; using
        only the title as a key silently drops the later card.
        """
        found: dict[tuple[str, tuple[int, int, int, int]], tuple[str, tuple[int, int, int, int]]] = {}
        for node in nodes:
            value = _clean_product_title(node.text or node.content_desc)
            if not node.bounds or not _looks_like_product(value):
                continue
            left, top, right, bottom = node.bounds
            # 抖音营销标签通常是 300~400px 宽、约 50px 高，而商品标题
            # 是横跨商品卡的长文本（当前真机约 713px 宽）。只排除“窄且矮”
            # 的节点，避免把未来可能出现的短商品名一概过滤掉。
            if right - left < 500 and bottom - top < 60:
                continue
            key = DouyinExtractor._title_key(value)
            if not key:
                continue
            found.setdefault((key, node.bounds), (value, node.bounds))
        return sorted(found.values(), key=lambda item: (item[1][1], item[1][0]))

    @staticmethod
    def _card_for_bounds(
        bounds: tuple[int, int, int, int],
        title_nodes: list[tuple[str, tuple[int, int, int, int]]],
    ) -> tuple[int, str, tuple[int, int, int, int]] | None:
        """Return the visible product card containing a field node/detection.

        A global nearest-title join is fragile when a card contains marketing
        badges or when two adjacent cards have similar titles.  The list page
        is single-column, so the vertical interval from one title to the next
        is a stable, cheap card boundary.  The small top padding also includes
        the thumbnail and badges above a title.
        """
        if not title_nodes:
            return None
        left, top, right, bottom = bounds
        center_y = (top + bottom) / 2
        center_x = (left + right) / 2
        for index, (title, title_bounds) in enumerate(title_nodes):
            title_left, title_top, title_right, _title_bottom = title_bounds
            next_top = (
                title_nodes[index + 1][1][1]
                if index + 1 < len(title_nodes)
                else title_top + 900
            )
            if not (title_top - 100 <= center_y < next_top):
                continue
            # Ignore unrelated controls far away from the product information
            # column while allowing price/sales nodes to sit beside the title.
            if center_x < title_left - 180 or center_x > title_right + 180:
                continue
            return index, title, title_bounds
        return None

    @staticmethod
    def _nearest_title_above(
        bounds: tuple[int, int, int, int],
        title_nodes: list[tuple[str, tuple[int, int, int, int]]],
        max_gap: int = 480,
    ) -> tuple[str, tuple[int, int, int, int]] | None:
        """Join a price/sales observation to the closest product title above it.

        Douyin's accessibility tree lists all text nodes in card order, but optional
        badges can appear between a title and its price.  Choosing the longest title
        in a text window can therefore steal the next card's price.  Coordinates are
        stable for the visible card and make the association deterministic.
        """
        left, top, right, _bottom = bounds
        center_x = (left + right) / 2
        candidates: list[tuple[float, str, tuple[int, int, int, int]]] = []
        for title, title_bounds in title_nodes:
            title_left, _title_top, title_right, title_bottom = title_bounds
            gap = top - title_bottom
            if gap < -30 or gap > max_gap:
                continue
            horizontal_gap = 0 if title_left <= center_x <= title_right else min(
                abs(center_x - title_left), abs(center_x - title_right)
            )
            candidates.append((gap + horizontal_gap / 4, title, title_bounds))
        return (
            min(candidates, key=lambda item: (item[0], item[2][1], -len(item[1])))[1:]
            if candidates
            else None
        )

    @staticmethod
    def _sort_products_by_card_position(products: list[ProductRecord]) -> None:
        """Keep the physical card order assigned during coordinate binding.

        ``ProductRecord.position`` is the unique card index from the current
        viewport.  A title-to-top lookup is not safe: several independent
        cards can have the same title, and mapping all of them to the first
        title coordinate moves intervening products out of order.  That in
        turn breaks the adjacent-screen overlap calculation.
        """
        products.sort(key=lambda product: product.position or 10**9)
        for index, product in enumerate(products, start=1):
            product.position = index

    @staticmethod
    def _set_sales(product: ProductRecord, raw_sales: str, method: str, confidence: float) -> None:
        product.displayed_sales = parse_count(raw_sales)
        product.displayed_sales_raw = raw_sales
        product.fields = [field for field in product.fields if field.key != "displayed_sales"]
        product.fields.append(
            FieldValue(
                key="displayed_sales",
                value=parse_count(raw_sales),
                raw_value=raw_sales,
                method=method,
                confidence=confidence,
            )
        )

    @staticmethod
    def _products(
        texts: list[str],
        nodes: list[UiNode] | None = None,
        ocr_detections: list[OcrDetection] | None = None,
    ) -> list[ProductRecord]:
        if nodes is None:
            return DouyinExtractor._products_from_texts(texts)

        title_nodes = DouyinExtractor._title_nodes(nodes)
        products_by_card: dict[int, ProductRecord] = {}
        for node in sorted(
            nodes,
            key=lambda item: (item.bounds[1], item.bounds[0]) if item.bounds else (10**9, 10**9),
        ):
            value = _clean_text(node.text or node.content_desc)
            price = parse_price(value)
            if not node.bounds or price is None or ("¥" not in value and "￥" not in value):
                continue
            associated = DouyinExtractor._card_for_bounds(node.bounds, title_nodes)
            if associated is None:
                continue
            card_index, title, _title_bounds = associated
            product = products_by_card.get(card_index)
            if product is None:
                product = ProductRecord(
                    title=title,
                    price=price,
                    position=card_index + 1,
                    fields=[
                        FieldValue(
                            key="price",
                            value=price,
                            raw_value=value,
                            method="ui",
                            confidence=0.9,
                        )
                    ],
                )
                products_by_card[card_index] = product
            elif product.price is None:
                product.price = price

        products = [products_by_card[index] for index in sorted(products_by_card)]
        # UI XML exposes sales in fixtures, while the real app usually exposes them
        # only through OCR.  Associate each observation with its card interval.
        for node in nodes:
            value = _clean_text(node.text or node.content_desc)
            sales_match = _SALES_RE.search(value)
            if not sales_match:
                continue
            raw_sales = sales_match.group(1)
            associated = DouyinExtractor._card_for_bounds(node.bounds, title_nodes) if node.bounds else None
            if associated is None:
                continue
            card_index, _title, _title_bounds = associated
            product = products_by_card.get(card_index)
            if product is not None:
                DouyinExtractor._set_sales(product, raw_sales, "ui", 0.85)

        # OCR is only used for fields absent from the accessibility tree.  The
        # card index prevents a merged OCR line from stealing the neighboring
        # card's price or sales, including duplicate visible titles.
        for detection in ocr_detections or []:
            sales_match = _SALES_RE.search(detection.text)
            if not sales_match:
                continue
            associated = DouyinExtractor._card_for_bounds(detection.bounds, title_nodes)
            if associated is None:
                continue
            card_index, title, _title_bounds = associated
            product = products_by_card.get(card_index)
            if product is None:
                ocr_price = parse_price(detection.text)
                product = ProductRecord(
                    title=title,
                    price=ocr_price,
                    position=card_index + 1,
                )
                products_by_card[card_index] = product
            DouyinExtractor._set_sales(
                product,
                sales_match.group(1),
                "ocr",
                detection.confidence,
            )

        products = [products_by_card[index] for index in sorted(products_by_card)]

        # OCR configured without boxes still arrives as plain text.  Fill only an
        # already-associated product so it cannot change the card order.
        plain_sales_occurrence: dict[str, int] = {}
        for index, value in enumerate(texts):
            sales_match = _SALES_RE.search(value)
            if not sales_match:
                continue
            title = next(
                (
                    previous
                    for previous in reversed(texts[max(0, index - 4) : index])
                    if _looks_like_product(previous)
                ),
                "",
            )
            matching = [
                product
                for product in products
                if DouyinExtractor._title_key(product.title)
                == DouyinExtractor._title_key(title)
            ]
            if matching:
                key = DouyinExtractor._title_key(title)
                occurrence = plain_sales_occurrence.get(key, 0)
                if occurrence < len(matching):
                    DouyinExtractor._set_sales(
                        matching[occurrence], sales_match.group(1), "ui", 0.85
                    )
                    plain_sales_occurrence[key] = occurrence + 1

        if not products:
            products = DouyinExtractor._products_from_texts(texts)
        DouyinExtractor._sort_products_by_card_position(products)
        return products

    @staticmethod
    def _products_from_texts(texts: list[str]) -> list[ProductRecord]:
        """Fallback for callers without UI bounds; normal extraction uses coordinates."""
        products: list[ProductRecord] = []
        for index, text in enumerate(texts):
            sales_match = _SALES_RE.search(text)
            if not sales_match:
                continue
            title = next(
                (
                    previous
                    for previous in reversed(texts[max(0, index - 4) : index])
                    if _looks_like_product(previous)
                ),
                "",
            )
            if not title:
                fallback = text.split("已售", 1)[0].strip(" -|·")
                title = fallback if _looks_like_product(fallback) else ""
            key = DouyinExtractor._title_key(title)
            if not key:
                continue
            price = next(
                (
                    parse_price(candidate)
                    for candidate in texts[max(0, index - 3) : index + 1]
                    if "已售" not in candidate and parse_price(candidate) is not None
                ),
                None,
            )
            product = ProductRecord(title=_clean_text(title), price=price)
            DouyinExtractor._set_sales(product, sales_match.group(1), "ui", 0.85)
            products.append(product)
        return products
