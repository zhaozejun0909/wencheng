from __future__ import annotations

import hashlib
import re
from pathlib import Path

from wen.extract.ui_xml import parse_ui_xml
from wen.models import ProductRecord


def capture_product_images(
    xml: str,
    screenshot: Path,
    products: list[ProductRecord],
    destination: Path,
) -> int:
    """Crop visible product thumbnails from one list-page screenshot.

    Douyin's product cards expose their image and title as separate nodes. We associate a
    title with the nearest sizeable ImageView immediately to its left, then crop once from
    the already captured full-screen image. This avoids opening every product detail page.
    """
    if not products or not screenshot.exists():
        return 0
    try:
        from PIL import Image
    except ImportError:
        return 0

    with Image.open(screenshot) as source:
        source = source.convert("RGB")
        if source.width < 100 or source.height < 100:
            return 0
        nodes = parse_ui_xml(xml)
        ui_width = max(source.width, max((node.bounds[2] for node in nodes if node.bounds), default=0))
        ui_height = max(source.height, max((node.bounds[3] for node in nodes if node.bounds), default=0))
        scale_x = source.width / max(1, ui_width)
        scale_y = source.height / max(1, ui_height)
        image_nodes = [
            node
            for node in nodes
            if node.bounds
            and node.class_name.endswith("ImageView")
            and _usable_image_bounds(node.bounds, ui_width, ui_height)
        ]
        if not image_nodes:
            return 0

        title_nodes = [node for node in nodes if node.bounds and (node.text or node.content_desc)]
        safe_content_top = _product_list_safe_top(nodes, ui_height)
        used_title_bounds: set[tuple[int, int, int, int]] = set()
        captured = 0
        destination.mkdir(parents=True, exist_ok=True)
        for product in products:
            title_bounds = _find_title_bounds(product.title, title_nodes, used_title_bounds)
            if not title_bounds:
                continue
            used_title_bounds.add(title_bounds)
            image_bounds = _nearest_image(title_bounds, image_nodes)
            if not image_bounds:
                continue
            left, top, right, bottom = image_bounds
            crop_box = (
                max(0, round(left * scale_x)),
                max(0, round(top * scale_y)),
                min(source.width, round(right * scale_x)),
                min(source.height, round(bottom * scale_y)),
            )
            crop_width = crop_box[2] - crop_box[0]
            crop_height = crop_box[3] - crop_box[1]
            if crop_width < 40 or crop_height < 40:
                continue
            # The sticky 商品/sort header overlays the first card. UiAutomator
            # still reports the covered ImageView's original bounds, so a crop
            # can be nearly square while containing the Tab itself. Defer that
            # physical card to the adjacent viewport where it is fully visible.
            if safe_content_top and top < safe_content_top:
                continue
            # Store thumbnails are square.  A much shorter crop means the card
            # is clipped by the screen edge; let the following viewport provide
            # the complete image instead of persisting a misleading fragment.
            if crop_height < crop_width * 0.95:
                continue
            candidate = source.crop(crop_box)
            # A visible card is the identity here.  Do not infer that matching
            # title, price or pixels mean the same business product: Douyin may
            # render multiple independently sold cards with identical data.
            filename = _image_filename(product.title, screenshot, image_bounds)
            path = destination / filename
            if not path.exists():
                candidate.save(path, format="JPEG", quality=86, optimize=True)
            product.image_path = filename
            captured += 1
    return captured


def capture_favorite_product_images(
    screenshot: Path,
    products: list[ProductRecord],
    card_bounds: list[tuple[int, int, int, int]],
    destination: Path,
) -> int:
    """Crop main-image regions from the canvas-rendered 商品收藏 cards.

    Current Douyin builds do not expose the card thumbnail as an ``ImageView`` in
    UIAutomator XML.  They do expose the row's ``删除`` action, however, so the
    collector passes those dynamic row bounds here.  The left-hand portion of a
    card is the product thumbnail; cropping it from the already captured screen
    avoids opening each detail page and remains valid when card heights change.
    """
    if not products or not card_bounds or not screenshot.exists():
        return 0
    try:
        from PIL import Image
    except ImportError:
        return 0

    destination.mkdir(parents=True, exist_ok=True)
    captured = 0
    with Image.open(screenshot) as source:
        source = source.convert("RGB")
        if source.width < 100 or source.height < 100:
            return 0
        # Card thumbnails occupy roughly the left quarter of a full-width
        # favorite row.  Use screen proportions, not a device-specific pixel
        # coordinate, so this also works on other phone resolutions.
        thumbnail_right = min(source.width, max(140, round(source.width * 0.30)))
        for product, bounds in zip(products, card_bounds, strict=False):
            _left, top, _right, bottom = bounds
            crop_box = (
                0,
                max(0, min(source.height, top)),
                thumbnail_right,
                max(0, min(source.height, bottom)),
            )
            if crop_box[3] - crop_box[1] < 40 or crop_box[2] - crop_box[0] < 40:
                continue
            candidate = source.crop(crop_box)
            filename = _image_filename(product.title, screenshot, bounds)
            path = destination / filename
            if not path.exists():
                candidate.save(path, format="JPEG", quality=86, optimize=True)
            product.image_path = filename
            captured += 1
    return captured


def capture_product_detail_image(
    xml: str,
    screenshot: Path,
    product: ProductRecord,
    destination: Path,
) -> bool:
    """Crop the first gallery image exposed by a Douyin product-detail page."""
    if not screenshot.exists():
        return False
    try:
        from PIL import Image
    except ImportError:
        return False

    candidates = [
        node.bounds
        for node in parse_ui_xml(xml)
        if node.bounds
        and (node.content_desc or node.text).strip() in {"图片1", "商品图片1"}
    ]
    if not candidates:
        return False
    # The hero image is the largest matching node; nested accessibility nodes
    # can repeat the same label with a smaller clipping rectangle.
    left, top, right, bottom = max(
        candidates,
        key=lambda value: (value[2] - value[0]) * (value[3] - value[1]),
    )
    nodes = parse_ui_xml(xml)
    with Image.open(screenshot) as source:
        source = source.convert("RGB")
        # UIAutomator may expose the next page of Douyin's horizontal image
        # carousel outside the visible screen (for example x=1280..2560).
        # Taking the maximum bound across the whole hierarchy therefore makes
        # the viewport look twice as wide and crops only the left half of the
        # real hero image.  Derive the viewport from origin-anchored nodes that
        # still fit inside the actual screenshot instead.
        ui_width = max(
            (
                node.bounds[2]
                for node in nodes
                if node.bounds
                and node.bounds[0] == 0
                and 0 < node.bounds[2] <= source.width
            ),
            default=source.width,
        )
        ui_height = max(
            (
                node.bounds[3]
                for node in nodes
                if node.bounds
                and node.bounds[1] == 0
                and 0 < node.bounds[3] <= source.height
            ),
            default=source.height,
        )
        crop_box = (
            max(0, round(left * source.width / ui_width)),
            max(0, round(top * source.height / ui_height)),
            min(source.width, round(right * source.width / ui_width)),
            min(source.height, round(bottom * source.height / ui_height)),
        )
        if crop_box[2] - crop_box[0] < 100 or crop_box[3] - crop_box[1] < 100:
            return False
        destination.mkdir(parents=True, exist_ok=True)
        identity = product.product_id or product.title
        filename = f"detail_{re.sub(r'[^0-9A-Za-z_-]+', '_', identity)[:80]}.jpg"
        source.crop(crop_box).save(destination / filename, format="JPEG", quality=88, optimize=True)
        product.image_path = filename
    return True


def _usable_image_bounds(
    bounds: tuple[int, int, int, int], ui_width: int, ui_height: int
) -> bool:
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    # Exclude full-screen decorative ImageViews and tiny icons/avatar images.
    return width >= 120 and height >= 120 and width < ui_width * 0.8 and height < ui_height * 0.8


def _product_list_safe_top(nodes, ui_height: int) -> int:
    """Return the first unobscured row below Douyin's sticky sort controls."""
    labels = {"综合", "销量", "上新", "价格"}
    controls = [
        node.bounds
        for node in nodes
        if node.bounds and (node.text or node.content_desc).strip() in labels
    ]
    for candidate in controls:
        center_y = (candidate[1] + candidate[3]) // 2
        same_row = [
            bounds
            for bounds in controls
            if abs(((bounds[1] + bounds[3]) // 2) - center_y) <= 70
        ]
        if len(same_row) >= 3:
            # XML image bounds beginning above the control row are genuinely
            # covered by the sticky Tab (observed y=441 vs row bottom 530).
            # Do not require an extra visual gap: a valid live first card has
            # also been observed at y=533, only three pixels below the row.
            return min(ui_height, max(bounds[3] for bounds in same_row))
    return 0


def _find_title_bounds(
    title: str,
    nodes,
    used_bounds: set[tuple[int, int, int, int]] | None = None,
) -> tuple[int, int, int, int] | None:
    needle = _normalize(title)
    used_bounds = used_bounds or set()
    exact = [
        node.bounds
        for node in nodes
        if node.bounds
        and node.bounds not in used_bounds
        and _normalize(node.text or node.content_desc) == needle
    ]
    if exact:
        return min(exact, key=lambda value: (value[1], -value[2] + value[0]))
    partial = [
        node.bounds
        for node in nodes
        if node.bounds
        and node.bounds not in used_bounds
        and needle
        and needle in _normalize(node.text or node.content_desc)
    ]
    return min(partial, key=lambda value: (value[1], -value[2] + value[0])) if partial else None


def _nearest_image(
    title_bounds: tuple[int, int, int, int], image_nodes
) -> tuple[int, int, int, int] | None:
    title_left, title_top, _title_right, title_bottom = title_bounds
    title_center = (title_top + title_bottom) / 2
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for node in image_nodes:
        _left, top, right, bottom = node.bounds
        if right > title_left + 80:
            continue
        overlap = max(0, min(bottom, title_bottom) - max(top, title_top))
        if overlap == 0 and abs((top + bottom) / 2 - title_center) > (bottom - top) * 0.9:
            continue
        horizontal_gap = max(0, title_left - right)
        score = horizontal_gap + abs((top + bottom) / 2 - title_center) * 0.2
        candidates.append((score, node.bounds))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _image_filename(
    title: str,
    screenshot: Path,
    card_bounds: tuple[int, int, int, int],
) -> str:
    """Give every visible card occurrence its own image file.

    Screenshot name + card coordinates identify the observation used during
    viewport stitching. They deliberately do not identify a business product.
    """
    identity = hashlib.sha1()
    identity.update(_normalize(title).encode("utf-8"))
    identity.update(b"\0")
    identity.update(screenshot.name.encode("utf-8"))
    identity.update(b"\0")
    identity.update(",".join(str(value) for value in card_bounds).encode("ascii"))
    digest = identity.hexdigest()[:16]
    return f"product-{digest}.jpg"


def _normalize(value: str) -> str:
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", value or "").strip()
