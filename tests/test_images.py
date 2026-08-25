from pathlib import Path

from PIL import Image

from wen.images import (
    capture_favorite_product_images,
    capture_product_detail_image,
    capture_product_images,
)
from wen.models import ProductRecord


def test_capture_product_image_matches_title_to_left_image(tmp_path: Path) -> None:
    screenshot = tmp_path / "products.png"
    Image.new("RGB", (400, 400), (220, 80, 80)).save(screenshot)
    xml = """
    <hierarchy>
      <node class="android.widget.ImageView" bounds="[0,0][180,180]" />
      <node class="android.widget.TextView" text="测试商品羽绒服" bounds="[200,30][390,90]" />
    </hierarchy>
    """
    product = ProductRecord(title="测试商品羽绒服")
    assert capture_product_images(xml, screenshot, [product], tmp_path) == 1
    assert product.image_path
    image_path = tmp_path / product.image_path
    assert image_path.exists()
    assert Image.open(image_path).size == (180, 180)


def test_identical_visible_cards_still_get_distinct_image_files(
    tmp_path: Path,
) -> None:
    xml = """
    <hierarchy>
      <node class="android.widget.ImageView" bounds="[0,0][180,180]" />
      <node class="android.widget.TextView" text="同名独立商品" bounds="[200,30][390,90]" />
      <node class="android.widget.ImageView" bounds="[0,220][180,400]" />
      <node class="android.widget.TextView" text="同名独立商品" bounds="[200,250][390,310]" />
    </hierarchy>
    """
    screenshot = tmp_path / "products.png"
    Image.new("RGB", (400, 440), (220, 80, 80)).save(screenshot)
    first = ProductRecord(title="同名独立商品", price=195)
    second = ProductRecord(title="同名独立商品", price=195)

    assert capture_product_images(xml, screenshot, [first, second], tmp_path) == 2

    assert first.image_path
    assert second.image_path
    assert first.image_path != second.image_path
    assert (tmp_path / first.image_path).exists()
    assert (tmp_path / second.image_path).exists()


def test_capture_product_image_ignores_bottom_edge_fragment_then_saves_full_crop(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "products.png"
    Image.new("RGB", (400, 400), (220, 80, 80)).save(screenshot)
    partial_xml = """
    <hierarchy>
      <node class="android.widget.ImageView" bounds="[0,330][180,400]" />
      <node class="android.widget.TextView" text="边缘商品羽绒服" bounds="[200,340][390,390]" />
    </hierarchy>
    """
    product = ProductRecord(title="边缘商品羽绒服")
    assert capture_product_images(partial_xml, screenshot, [product], tmp_path) == 0
    assert product.image_path is None

    full_xml = """
    <hierarchy>
      <node class="android.widget.ImageView" bounds="[0,100][180,280]" />
      <node class="android.widget.TextView" text="边缘商品羽绒服" bounds="[200,130][390,190]" />
    </hierarchy>
    """
    assert capture_product_images(full_xml, screenshot, [product], tmp_path) == 1
    assert product.image_path
    assert Image.open(tmp_path / product.image_path).size == (180, 180)


def test_capture_product_image_ignores_card_covered_by_sticky_sort_tab(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "scroll.png"
    Image.new("RGB", (400, 600), (220, 80, 80)).save(screenshot)
    xml = """
    <hierarchy bounds="[0,0][400,600]">
      <node text="综合" bounds="[20,90][80,130]" />
      <node text="销量" bounds="[90,90][150,130]" />
      <node text="上新" bounds="[160,90][220,130]" />
      <node text="价格" bounds="[230,90][290,130]" />
      <node class="android.widget.ImageView" bounds="[0,100][180,275]" />
      <node class="android.widget.TextView" text="顶部被挡商品羽绒服" bounds="[200,135][390,195]" />
    </hierarchy>
    """
    product = ProductRecord(title="顶部被挡商品羽绒服")
    assert capture_product_images(xml, screenshot, [product], tmp_path) == 0
    assert product.image_path is None


def test_capture_product_image_keeps_first_full_card_below_sticky_sort_tab(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "first-card.png"
    Image.new("RGB", (400, 600), (80, 150, 220)).save(screenshot)
    xml = """
    <hierarchy bounds="[0,0][400,600]">
      <node text="综合" bounds="[20,90][80,130]" />
      <node text="销量" bounds="[90,90][150,130]" />
      <node text="上新" bounds="[160,90][220,130]" />
      <node text="价格" bounds="[230,90][290,130]" />
      <node class="android.widget.ImageView" bounds="[0,133][180,313]" />
      <node class="android.widget.TextView" text="列表第一件完整羽绒服" bounds="[200,147][390,207]" />
    </hierarchy>
    """
    product = ProductRecord(title="列表第一件完整羽绒服")
    assert capture_product_images(xml, screenshot, [product], tmp_path) == 1
    assert product.image_path is not None


def test_capture_favorite_product_image_uses_dynamic_card_row(tmp_path: Path) -> None:
    screenshot = tmp_path / "favorites.png"
    Image.new("RGB", (400, 500), (80, 150, 220)).save(screenshot)
    product = ProductRecord(title="收藏商品羽绒服")
    rows = [(320, 40, 390, 240)]
    assert capture_favorite_product_images(screenshot, [product], rows, tmp_path) == 1
    assert product.image_path
    image_path = tmp_path / product.image_path
    assert image_path.exists()
    with Image.open(image_path) as image:
        assert image.width >= 120
        assert image.height == 200


def test_detail_image_ignores_offscreen_horizontal_carousel_page(tmp_path: Path) -> None:
    screenshot = tmp_path / "detail.png"
    image = Image.new("RGB", (400, 600), (220, 80, 80))
    for x in range(200, 400):
        for y in range(50, 450):
            image.putpixel((x, y), (80, 120, 220))
    image.save(screenshot)
    xml = """
    <hierarchy bounds="[0,0][400,600]">
      <node content-desc="图片1" bounds="[0,50][400,450]" />
      <node class="android.widget.FrameLayout" bounds="[400,0][800,600]" />
    </hierarchy>
    """
    product = ProductRecord(title="轮播主图商品", product_id="123456789")

    assert capture_product_detail_image(xml, screenshot, product, tmp_path)
    assert product.image_path
    with Image.open(tmp_path / product.image_path) as captured:
        assert captured.size == (400, 400)
        red, green, blue = captured.getpixel((350, 200))
        assert red < 100 and 100 < green < 140 and blue > 200
