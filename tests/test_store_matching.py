from wen.extract import OcrDetection
from wen.models import CollectionResult, JobStatus, ProductRecord, ProductSelection, ProductSortMode
from wen.workflows.collector import Collector


def test_store_candidates_ignore_search_input_and_non_store_promotions() -> None:
    xml = """
    <hierarchy>
      <node class="android.widget.EditText" text="鸭鸭童装旗舰店" bounds="[143,165][927,295]" />
    </hierarchy>
    """
    detections = [
        OcrDetection("鸭鸭童装旗舰店", (260, 190, 700, 240), 0.99),
        OcrDetection("鸭鸭童装官方旗舰店", (260, 700, 700, 750), 0.99),
        OcrDetection("抖音店铺榜·品牌男童羽绒服店铺榜TOP8", (20, 1000, 700, 1040), 0.99),
    ]

    candidates = Collector._store_candidates(xml, detections)

    assert [name for name, _bounds in candidates] == ["鸭鸭童装官方旗舰店"]
    assert Collector._select_store_candidate(candidates, "鸭鸭童装官方旗舰店") == (
        260,
        700,
        700,
        750,
    )
    assert Collector._select_store_candidate(candidates, "鸭鸭童装旗舰店") is None


def test_store_wait_candidate_ignores_search_edit_text() -> None:
    loading_xml = """
    <hierarchy>
      <node class="android.widget.EditText" text="鸭鸭童装官方旗舰店"
            bounds="[143,165][927,295]" />
      <node text="店铺" selected="true" bounds="[420,350][520,420]" />
    </hierarchy>
    """
    card_xml = """
    <hierarchy>
      <node class="android.widget.EditText" text="鸭鸭童装官方旗舰店"
            bounds="[143,165][927,295]" />
      <node text="店铺" selected="true" bounds="[420,350][520,420]" />
      <node class="android.widget.TextView" text="鸭鸭童装官方旗舰店"
            bounds="[80,620][760,700]" />
    </hierarchy>
    """

    assert not Collector._xml_has_store_candidate(loading_xml, "鸭鸭童装官方旗舰店")
    assert Collector._xml_has_store_candidate(card_xml, "鸭鸭童装官方旗舰店")


def test_store_entry_prefers_same_card_enter_button() -> None:
    name_bounds = (268, 673, 699, 723)
    detections = [
        OcrDetection("进店", (1068, 734, 1161, 788), 0.99),
        OcrDetection("进店", (1067, 1524, 1161, 1577), 0.99),
    ]

    assert Collector._store_entry_bounds(name_bounds, detections, "") == (
        1068,
        734,
        1161,
        788,
    )


def test_store_matching_only_normalizes_whitespace() -> None:
    candidates = [("鸭鸭童装官方旗舰店", (1, 2, 3, 4))]

    assert Collector._select_store_candidate(candidates, " 鸭鸭童装 官方旗舰店 ") == (
        1,
        2,
        3,
        4,
    )
    assert Collector._select_store_candidate(candidates, "鸭鸭童装旗舰店") is None


def test_requested_product_matching_uses_title_substrings() -> None:
    products = [ProductRecord(title="鸭鸭儿童羽绒服冬季轻薄款", price=109)]

    assert Collector._requested_products_found(products, ["儿童羽绒服"])
    assert not Collector._requested_products_found(products, ["儿童羽绒服", "卫衣"])


def test_precise_selection_uses_title_and_price_and_keeps_first_duplicate() -> None:
    products = [
        ProductRecord(title="同名羽绒服", price=299),
        ProductRecord(title="同名羽绒服", price=329),
        ProductRecord(title="同名羽绒服", price=329),
    ]
    result = CollectionResult(
        job_id="test", status=JobStatus.RUNNING, backend="appium", keyword="店铺"
    )
    selected = Collector._filter_products(
        products,
        [ProductSelection(title="同名羽绒服", price=329)],
        None,
        None,
        result,
    )
    assert len(selected) == 1
    assert selected[0] is products[1]


def test_native_price_order_is_not_reordered_locally() -> None:
    products = [
        ProductRecord(title="未知价格商品"),
        ProductRecord(title="高价商品", price=300),
        ProductRecord(title="低价商品", price=100),
    ]

    assert Collector._sort_products(products, ProductSortMode.PRICE_ASC) is products
    assert Collector._sort_products(products, ProductSortMode.PRICE_DESC) is products


def test_native_sales_order_is_not_reordered_by_ocr_values() -> None:
    products = [
        ProductRecord(title="页面第一项", displayed_sales=100),
        ProductRecord(title="页面第二项", displayed_sales=10000),
    ]
    assert Collector._sort_products(products, ProductSortMode.SALES) is products
