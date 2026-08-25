from pathlib import Path

from fixture_device import FixtureDevice
from PIL import Image

from wen.config import Settings
from wen.extract import OcrDetection
from wen.extract.ui_xml import find_exact_text_bounds, find_text_bounds
from wen.models import (
    FieldValue,
    JobStatus,
    PreciseQueryMode,
    ProductRecord,
    ProductSelectionMode,
    ProductSortMode,
)
from wen.storage import DataStore
from wen.workflows.collector import Collector


def test_fixture_collection_writes_evidence_and_db(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    store = DataStore(settings.database_path)
    result = Collector(settings, FixtureDevice(Path("fixtures/douyin_shop.xml")), store).run(
        "鸭鸭童装旗舰店", 2
    )
    assert result.status == JobStatus.SUCCEEDED
    assert result.store and result.store.name == "鸭鸭童装旗舰店"
    assert len(result.products) == 2
    assert result.evidence_dir and (Path(result.evidence_dir) / "01_start.xml").exists()
    assert store.get_result(result.job_id)
    csv_path = store.export_csv(result.job_id, tmp_path / "exports" / "result.csv")
    xlsx_path = store.export_xlsx(result.job_id, tmp_path / "exports" / "result.xlsx")
    assert csv_path.exists()
    assert xlsx_path.exists()


def test_product_id_precision_opens_details_directly_and_uses_detail_fields(
    tmp_path: Path,
) -> None:
    class DetailDevice(FixtureDevice):
        is_live = True

        def __init__(self) -> None:
            super().__init__()
            self.opened: list[str] = []

        def open_uri(self, uri: str, package: str | None = None) -> None:
            self.opened.append(uri)

        def dump_ui(self, destination: Path) -> Path:
            product_id = self.opened[-1].split("product_id=", 1)[1].split("&", 1)[0]
            xml = f'''<hierarchy bounds="[0,0][1280,2772]">
              <node content-desc="图片1" bounds="[0,152][1280,1432]" />
              <node text="券后价" bounds="[900,2546][1069,2608]" />
              <node text="¥105" bounds="[1076,2554][1180,2602]" />
              <node text="已售61" bounds="[55,1600][176,1655]" />
              <node text="鸭鸭商品 {product_id}" bounds="[65,1892][1215,2046]" />
              <node text="加购" bounds="[310,2547][412,2675]" />
            </hierarchy>'''
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(xml, encoding="utf-8")
            return destination

        def screenshot(self, destination: Path) -> Path:
            Image.new("RGB", (1280, 2772), "white").save(destination)
            return destination

    settings = Settings(data_dir=tmp_path, min_action_interval=0.1)
    settings.ensure_dirs()
    device = DetailDevice()
    result = Collector(settings, device, DataStore(settings.database_path)).run(
        "商品 ID 查询",
        selection_mode=ProductSelectionMode.PRECISE,
        precise_query_mode=PreciseQueryMode.PRODUCT_IDS,
        product_ids=["3817041292878283160", "3832268942550892829"],
    )

    assert result.status == JobStatus.SUCCEEDED
    assert not device.started
    assert [product.product_id for product in result.products] == [
        "3817041292878283160",
        "3832268942550892829",
    ]
    assert [product.price for product in result.products] == [105, 105]
    assert [product.displayed_sales for product in result.products] == [61, 61]
    assert all(product.image_path for product in result.products)
    assert all("promotion_id=" in uri for uri in device.opened)


def test_product_id_query_does_not_capture_stale_previous_detail(tmp_path: Path) -> None:
    class TransitioningDetailDevice(FixtureDevice):
        is_live = True

        def __init__(self) -> None:
            super().__init__()
            self.pending_id = ""
            self.visible_id = ""
            self.dumps_after_open = 0
            self.screenshot_ids: list[str] = []

        def open_uri(self, uri: str, package: str | None = None) -> None:
            self.pending_id = uri.split("product_id=", 1)[1].split("&", 1)[0]
            self.dumps_after_open = 0
            if not self.visible_id:
                self.visible_id = self.pending_id

        def dump_ui(self, destination: Path) -> Path:
            # A real deep link briefly leaves the preceding detail hierarchy
            # visible.  Only the next hierarchy belongs to the requested ID.
            if self.pending_id != self.visible_id and self.dumps_after_open >= 1:
                self.visible_id = self.pending_id
            self.dumps_after_open += 1
            xml = f'''<hierarchy bounds="[0,0][1280,2772]" window-id="{self.visible_id}">
              <node content-desc="图片1" bounds="[0,152][1280,1432]" />
              <node text="券后价" bounds="[900,2546][1069,2608]" />
              <node text="¥105" bounds="[1076,2554][1180,2602]" />
              <node text="已售61" bounds="[55,1600][176,1655]" />
              <node text="鸭鸭商品 {self.visible_id}" bounds="[65,1892][1215,2046]" />
              <node text="加购" bounds="[310,2547][412,2675]" />
            </hierarchy>'''
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(xml, encoding="utf-8")
            return destination

        def screenshot(self, destination: Path) -> Path:
            self.screenshot_ids.append(self.visible_id)
            Image.new("RGB", (1280, 2772), "white").save(destination)
            return destination

    settings = Settings(data_dir=tmp_path, min_action_interval=0.1)
    settings.ensure_dirs()
    device = TransitioningDetailDevice()
    requested = ["3817041292878283160", "3832268942550892829"]
    result = Collector(settings, device, DataStore(settings.database_path)).run(
        "商品 ID 查询",
        selection_mode=ProductSelectionMode.PRECISE,
        precise_query_mode=PreciseQueryMode.PRODUCT_IDS,
        product_ids=requested,
    )

    assert result.status == JobStatus.SUCCEEDED
    assert device.screenshot_ids == requested
    assert [product.product_id for product in result.products] == requested


def test_delete_job_removes_result_and_snapshots(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, min_action_interval=0.1)
    settings.ensure_dirs()
    store = DataStore(settings.database_path)
    result = Collector(settings, FixtureDevice(Path("fixtures/douyin_shop.xml")), store).run(
        "鸭鸭童装旗舰店", 1
    )
    assert store.get_result(result.job_id) is not None
    assert store.delete_job(result.job_id)
    assert store.get_result(result.job_id) is None
    assert store.list_jobs() == []


def test_find_text_bounds_matches_content_description_substring() -> None:
    xml = '<hierarchy><node content-desc="关闭，按钮" bounds="[10,20][110,120]" /></hierarchy>'
    assert find_text_bounds(xml, "关闭") == (10, 20, 110, 120)


def test_find_exact_text_bounds_does_not_hit_shop_local_search_label() -> None:
    xml = (
        '<hierarchy><node text="搜索本店商品" bounds="[10,20][110,120]" />'
        '<node text="搜索" bounds="[120,20][220,120]" /></hierarchy>'
    )
    assert find_exact_text_bounds(xml, "搜索") == (120, 20, 220, 120)
    assert find_exact_text_bounds(xml, "Search") is None


def test_store_sort_control_is_resolved_from_its_four_item_row() -> None:
    xml = """
    <hierarchy>
      <node text="综合" bounds="[50,465][160,530]" />
      <node text="销量" bounds="[230,465][340,530]" />
      <node text="上新" bounds="[410,465][520,530]" />
      <node text="价格" bounds="[590,465][700,530]" />
      <node text="综合" bounds="[50,1600][200,1680]" />
    </hierarchy>
    """
    assert Collector._sort_control_bounds(xml, "综合") == (50, 465, 160, 530)
    assert Collector._sort_control_bounds(xml, "销量") == (230, 465, 340, 530)


def test_price_desc_taps_native_price_control_twice(
    tmp_path: Path, monkeypatch,
) -> None:
    class TapDevice(FixtureDevice):
        def __init__(self) -> None:
            super().__init__()
            self.taps: list[tuple[int, int]] = []

        def tap(self, x: int, y: int) -> None:
            self.taps.append((x, y))

    xml = """
    <hierarchy>
      <node text="综合" bounds="[50,465][160,530]" />
      <node text="销量" bounds="[230,465][340,530]" />
      <node text="上新" bounds="[410,465][520,530]" />
      <node text="价格" bounds="[590,465][700,530]" />
    </hierarchy>
    """
    monkeypatch.setattr("wen.workflows.collector.time.sleep", lambda _seconds: None)
    device = TapDevice()
    collector = Collector(
        Settings(data_dir=tmp_path), device, DataStore(tmp_path / "wen.sqlite3")
    )

    collector._prepare_product_list(ProductSortMode.PRICE_ASC, xml, tmp_path)
    assert device.taps == [(645, 497)]

    device.taps.clear()
    collector._prepare_product_list(ProductSortMode.PRICE_DESC, xml, tmp_path)
    assert device.taps == [(645, 497), (645, 497)]


def test_incomplete_viewport_edge_cards_are_deferred() -> None:
    products = [
        ProductRecord(title="顶部半卡"),
        ProductRecord(title="完整商品一", image_path="one.jpg"),
        ProductRecord(title="完整商品二", image_path="two.jpg"),
        ProductRecord(title="底部半卡"),
    ]
    kept = Collector._drop_incomplete_edge_products(products)
    assert [product.title for product in kept] == ["完整商品一", "完整商品二"]


def test_fixture_precise_catalog_reads_all_fixture_products(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, min_action_interval=0.1, max_scrolls=20)
    settings.ensure_dirs()
    result = Collector(settings, FixtureDevice(Path("fixtures/douyin_shop.xml")), DataStore(settings.database_path)).run(
        "鸭鸭童装旗舰店", 500, selection_mode=ProductSelectionMode.PRECISE_CATALOG
    )
    assert result.status == JobStatus.SUCCEEDED
    assert result.store and result.store.product_count == 36
    assert len(result.products) == 36


def test_precise_query_without_product_ids_no_longer_falls_back_to_store_matching(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, min_action_interval=0.1, max_scrolls=20)
    settings.ensure_dirs()
    store = DataStore(settings.database_path)
    device = FixtureDevice(Path("fixtures/douyin_shop.xml"))
    result = Collector(settings, device, store).run(
        "鸭鸭童装旗舰店",
        500,
        product_titles=["鸭鸭儿童羽绒服冬季保暖外套", "已经改名的保存商品"],
        selection_mode=ProductSelectionMode.PRECISE,
        query_group_name="重点款",
    )
    assert result.status == JobStatus.FAILED
    assert any("商品 ID" in error for error in result.errors)
    assert not device.started


def test_fixture_favorites_are_not_bound_to_a_store(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, min_action_interval=0.1, max_scrolls=20)
    settings.ensure_dirs()
    store = DataStore(settings.database_path)
    result = Collector(settings, FixtureDevice(Path("fixtures/douyin_shop.xml")), store).run(
        "我的收藏", 500, selection_mode=ProductSelectionMode.FAVORITES,
        query_group_name="全部收藏商品",
    )
    assert result.status == JobStatus.SUCCEEDED
    assert result.store is None
    assert result.requested_store_name is None
    assert len(result.products) == 36
    assert store.list_query_runs()[0]["groups"][0]["store_name"] == "我的收藏"


def test_favorite_invalid_sibling_marker_is_removed_by_card_coordinates() -> None:
    xml = (
        '<hierarchy><node text="收藏" bounds="[0,0][100,50]" />'
        '<node text="有效商品羽绒服" bounds="[60,500][800,580]" />'
        '<node text="已售 10" bounds="[60,590][300,640]" />'
        '<node text="失效商品羽绒服" bounds="[60,800][800,880]" />'
        '<node text="商品已失效" bounds="[60,890][300,940]" />'
        '<node text="¥99" bounds="[60,950][200,1000]" /></hierarchy>'
    )
    products = [ProductRecord(title="有效商品羽绒服"), ProductRecord(title="失效商品羽绒服")]
    valid, invalid = Collector._filter_invalid_favorites(products, ["收藏", "商品已失效"], xml)
    assert [item.title for item in valid] == ["有效商品羽绒服"]
    assert invalid == ["失效商品羽绒服"]


def test_invalid_favorite_state_does_not_remove_same_title_valid_card() -> None:
    title = "YAYA鸭鸭同名儿童羽绒服冬季加厚保暖商品"
    xml = (
        f'<hierarchy><node text="{title}" bounds="[60,500][800,580]" />'
        '<node text="已售 10" bounds="[60,590][300,640]" />'
        f'<node text="{title}" bounds="[60,800][800,880]" />'
        '<node text="商品已失效" bounds="[60,890][300,940]" /></hierarchy>'
    )
    products = [ProductRecord(title=title), ProductRecord(title=title)]
    valid, invalid = Collector._filter_invalid_favorites(
        products, ["收藏", "商品已失效"], xml
    )
    assert valid == [products[0]]
    assert invalid == [title]


def test_favorite_list_detection_rejects_preview_card() -> None:
    assert not Collector._is_favorite_product_list(["商品收藏", "查看全部"])
    assert Collector._is_favorite_product_list(["商品收藏", "鸭鸭儿童羽绒服", "¥109"])
    # Current builds draw the title and cards outside the accessibility tree; XML
    # still exposes the dedicated list controls and card state.
    assert Collector._is_favorite_product_list(["返回", "管理", "删除", "你可能还会喜欢"])
    assert not Collector._is_favorite_product_list(["返回", "管理", "查看全部"])


def test_favorites_all_filter_is_resolved_from_title_and_viewport() -> None:
    xml = (
        '<hierarchy bounds="[0,0][1280,2772]">'
        '<node text="商品收藏" bounds="[0,152][1280,295]" />'
        '</hierarchy>'
    )
    assert Collector._favorites_all_filter_point(xml) == (230, 392)
    assert Collector._favorites_all_filter_point(
        '<hierarchy bounds="[0,0][1280,2772]" />'
    ) is None


def test_favorite_list_content_wait_rejects_loading_skeleton() -> None:
    skeleton = '<hierarchy><node text="商品收藏" bounds="[0,0][1280,200]" /></hierarchy>'
    loaded = (
        '<hierarchy><node text="商品收藏" bounds="[0,0][1280,200]" />'
        '<node text="删除" bounds="[1026,490][1254,809]" /></hierarchy>'
    )
    assert not Collector._favorite_list_content_ready(skeleton, ["商品收藏"])
    assert Collector._favorite_list_content_ready(loaded, ["商品收藏", "删除"])


def test_favorite_cards_stop_at_recommendations_and_parse_only_collection_rows() -> None:
    xml = (
        '<hierarchy>'
        '<node text="删除" bounds="[1026,516][1254,835]" />'
        '<node text="删除" bounds="[1026,861][1254,1180]" />'
        '<node text="你可能还会喜欢" bounds="[0,1180][429,1380]" />'
        '</hierarchy>'
    )
    detections = [
        OcrDetection("YAYA鸭鸭2025新款中大童羽绒服", (553, 550, 1187, 606), 0.99),
        OcrDetection("￥239", (365, 750, 502, 806), 0.99),
        OcrDetection("已售2000+", (504, 750, 692, 806), 0.99),
        OcrDetection("鸭鸭儿童小童羽绒服", (551, 903, 1222, 945), 0.99),
        OcrDetection("￥109", (365, 1095, 492, 1149), 0.99),
        OcrDetection("已售50", (493, 1103, 622, 1147), 0.99),
        OcrDetection("推荐商品", (100, 1250, 500, 1300), 0.99),
    ]
    products, invalid = Collector._extract_favorite_products(xml, detections)
    assert [product.title for product in products] == [
        "YAYA鸭鸭2025新款中大童羽绒服",
        "鸭鸭儿童小童羽绒服",
    ]
    assert [product.displayed_sales for product in products] == [2000, 50]
    assert invalid == []
    assert Collector._favorite_collection_end_seen(xml, detections)
    assert Collector._favorite_card_bounds(
        '<hierarchy><node text="推荐商品" bounds="[0,0][100,100]" /></hierarchy>'
    ) == []


def test_favorite_title_slightly_above_delete_row_is_still_same_card() -> None:
    xml = (
        '<hierarchy>'
        '<node text="删除" bounds="[1026,490][1254,777]" />'
        '<node text="删除" bounds="[1026,784][1254,1103]" />'
        '</hierarchy>'
    )
    detections = [
        OcrDetection("商品已失效", (371, 610, 600, 662), 0.99),
        OcrDetection("鸭鸭儿童小童羽绒服冬季轻薄款", (551, 753, 1220, 801), 0.99),
        OcrDetection("￥105", (362, 947, 495, 1005), 0.99),
        OcrDetection("已售61", (496, 957, 612, 1001), 0.99),
    ]
    products, invalid, rows = Collector._extract_favorite_products_with_rows(
        xml, detections
    )
    assert [(item.title, item.price, item.displayed_sales) for item in products] == [
        ("鸭鸭儿童小童羽绒服冬季轻薄款", 105, 61)
    ]
    assert invalid == ["收藏页中的失效商品（标题未能读取）"]
    assert rows == [(1026, 784, 1254, 1103)]


def test_scrolled_favorites_do_not_count_leading_invalid_tail_twice() -> None:
    xml = (
        '<hierarchy>'
        '<node text="删除" bounds="[1026,490][1254,768]" />'
        '<node text="删除" bounds="[1026,751][1254,1070]" />'
        '<node text="删除" bounds="[1026,1059][1254,1378]" />'
        '<node text="删除" bounds="[1026,1385][1254,1704]" />'
        '</hierarchy>'
    )
    detections = [
        OcrDetection("商品已失效", (369, 564, 600, 616), 0.99),
        OcrDetection("鸭鸭儿童小童羽绒服冬季轻薄款", (551, 705, 1220, 754), 0.99),
        OcrDetection("￥105", (362, 899, 493, 958), 0.99),
        OcrDetection("已售61", (492, 908, 611, 952), 0.99),
        OcrDetection("经典甄选双人餐", (371, 1050, 980, 1100), 0.99),
        OcrDetection("商品已失效", (371, 1256, 598, 1303), 0.99),
        OcrDetection("爆款精品套餐", (373, 1398, 762, 1444), 0.99),
        OcrDetection("商品已失效", (369, 1595, 600, 1651), 0.99),
    ]
    products, invalid, _rows = Collector._extract_favorite_products_with_rows(
        xml,
        detections,
        defer_leading_untitled=True,
    )
    assert [(item.title, item.price, item.displayed_sales) for item in products] == [
        ("鸭鸭儿童小童羽绒服冬季轻薄款", 105, 61)
    ]
    assert invalid == ["经典甄选双人餐", "爆款精品套餐"]


def test_favorite_partial_card_fills_last_product_after_swipe() -> None:
    products = [ProductRecord(title="鸭鸭儿童小童羽绒服", price=None, displayed_sales=None)]
    xml = '<hierarchy><node text="删除" bounds="[1026,490][1254,620]" /></hierarchy>'
    detections = [
        OcrDetection("￥109", (365, 520, 492, 568), 0.99),
        OcrDetection("已售50", (493, 520, 622, 568), 0.99),
    ]
    assert Collector._merge_favorite_partial_row(products, xml, detections)
    assert products[0].price == 109
    assert products[0].displayed_sales == 50
    assert products[0].displayed_sales_raw == "50"


def test_favorite_extractor_defers_clipped_edge_rows() -> None:
    xml = (
        '<hierarchy>'
        '<node text="删除" bounds="[1026,490][1254,609]" />'
        '<node text="删除" bounds="[1026,635][1254,954]" />'
        '<node text="删除" bounds="[1026,980][1254,1299]" />'
        '</hierarchy>'
    )
    detections = [
        OcrDetection("顶部残片商品", (500, 510, 1100, 570), 0.99),
        OcrDetection("完整收藏商品", (500, 670, 1100, 730), 0.99),
        OcrDetection("¥105", (500, 850, 650, 900), 0.99),
        OcrDetection("已售60", (660, 850, 820, 900), 0.99),
        OcrDetection("另一个完整商品", (500, 1020, 1100, 1080), 0.99),
        OcrDetection("¥125", (500, 1190, 650, 1240), 0.99),
        OcrDetection("已售20", (660, 1190, 820, 1240), 0.99),
    ]
    products, invalid, rows = Collector._extract_favorite_products_with_rows(
        xml, detections
    )
    assert [product.title for product in products] == [
        "完整收藏商品",
        "另一个完整商品",
    ]
    assert invalid == []
    assert rows == [(1026, 635, 1254, 954), (1026, 980, 1254, 1299)]


def test_scroll_overlap_keeps_an_identical_adjacent_business_card() -> None:
    repeated = "YAYA鸭鸭2026儿童春秋新款连帽羽绒服轻薄保暖外套"
    coat = "YAYA鸭鸭貉子真毛领儿童中长款加厚保暖羽绒服"
    previous = [
        ProductRecord(title=coat, price=299, displayed_sales_raw="7万+"),
        ProductRecord(title=coat, price=329, displayed_sales_raw="2万+"),
        ProductRecord(title=repeated, price=109, displayed_sales_raw="2000+"),
    ]
    current = [
        ProductRecord(title=repeated, price=109, displayed_sales_raw="2000+"),
        ProductRecord(title=coat, price=329, displayed_sales_raw="1000+"),
        ProductRecord(title="鸭鸭儿童羽绒服2025", price=199, displayed_sales_raw="1000+"),
    ]
    before_xml = f"""
    <hierarchy>
      <node text="{coat}" bounds="[514,568][1227,702]" />
      <node text="¥299" bounds="[514,862][808,923]" />
      <node text="{coat}" bounds="[514,1055][1227,1189]" />
      <node text="¥329" bounds="[514,1349][808,1410]" />
      <node text="{repeated}" bounds="[514,1542][1227,1676]" />
      <node text="¥109" bounds="[514,1836][808,1897]" />
      <node text="{repeated}" bounds="[514,2029][1227,2163]" />
      <node text="¥109" bounds="[514,2323][808,2384]" />
      <node text="{coat}" bounds="[514,2516][1227,2650]" />
    </hierarchy>
    """
    current_xml = f"""
    <hierarchy>
      <node text="{repeated}" bounds="[514,610][1227,744]" />
      <node text="¥109" bounds="[514,904][808,965]" />
      <node text="{coat}" bounds="[514,1097][1227,1231]" />
      <node text="¥329" bounds="[514,1391][808,1452]" />
      <node text="鸭鸭儿童羽绒服2025" bounds="[514,1584][1227,1651]" />
      <node text="¥199" bounds="[514,1878][808,1939]" />
    </hierarchy>
    """

    # A text-only comparison sees one identical row and incorrectly removes it.
    assert Collector._viewport_overlap_count(previous, current) == 1
    # The 1461px gesture maps the new first card to the *second* identical
    # pre-scroll card, so it is a new business row and overlap is zero.
    assert Collector._viewport_overlap_count(
        previous,
        current,
        before_xml=before_xml,
        current_xml=current_xml,
        expected_scroll_delta=1461,
    ) == 0


def test_scroll_overlap_uses_distance_before_repeated_match_length() -> None:
    repeated = "YAYA鸭鸭貉子真毛领儿童中长款加厚保暖羽绒服"
    previous = [
        ProductRecord(title="首件商品", price=695),
        ProductRecord(title="第二件商品", price=325),
        ProductRecord(title=repeated, price=325, displayed_sales_raw="800+"),
        ProductRecord(title=repeated, price=325, displayed_sales_raw="100+"),
    ]
    current = [
        ProductRecord(title=repeated, price=325, displayed_sales_raw="100+"),
        ProductRecord(title=repeated, price=325, displayed_sales_raw="2万+"),
        ProductRecord(title=repeated, price=325, displayed_sales_raw="500+"),
        ProductRecord(title=repeated, price=325, displayed_sales_raw="1000+"),
    ]
    before_xml = f"""
    <hierarchy>
      <node text="首件商品" bounds="[514,568][1227,702]" /><node text="¥695" bounds="[514,862][808,923]" />
      <node text="第二件商品" bounds="[514,1055][1227,1189]" /><node text="¥325" bounds="[514,1349][808,1410]" />
      <node text="{repeated}" bounds="[514,1542][1227,1676]" /><node text="¥325" bounds="[514,1836][808,1897]" />
      <node text="{repeated}" bounds="[514,2029][1227,2163]" /><node text="¥325" bounds="[514,2323][808,2384]" />
      <node text="{repeated}" bounds="[514,2516][1227,2650]" />
    </hierarchy>
    """
    current_xml = f"""
    <hierarchy>
      <node text="{repeated}" bounds="[514,499][1227,633]" /><node text="¥325" bounds="[514,793][808,854]" />
      <node text="{repeated}" bounds="[514,986][1227,1120]" /><node text="¥325" bounds="[514,1280][808,1341]" />
      <node text="{repeated}" bounds="[514,1473][1227,1607]" /><node text="¥325" bounds="[514,1767][808,1828]" />
      <node text="{repeated}" bounds="[514,1960][1227,2094]" /><node text="¥325" bounds="[514,2254][808,2315]" />
    </hierarchy>
    """

    assert Collector._viewport_overlap_count(
        previous,
        current,
        before_xml=before_xml,
        current_xml=current_xml,
        expected_scroll_delta=1461,
    ) == 1


def test_overlapping_complete_card_fills_edge_card_price_and_image() -> None:
    edge = ProductRecord(
        title="YAYA鸭鸭2025儿童羽绒服男女童加厚保暖外套",
        price=None,
        displayed_sales=1000,
        displayed_sales_raw="1000+",
    )
    complete = ProductRecord(
        title=edge.title,
        price=239,
        displayed_sales=1000,
        displayed_sales_raw="1000+",
        image_path="product-complete.jpg",
        fields=[
            FieldValue(
                key="price",
                value=239,
                raw_value="¥239",
                method="ui",
                confidence=0.9,
            )
        ],
    )

    Collector._complete_product_observation(edge, complete)

    assert edge.price == 239
    assert edge.image_path == "product-complete.jpg"
    assert [(field.key, field.value) for field in edge.fields] == [("price", 239)]


def test_overlap_completion_keeps_existing_full_image() -> None:
    existing = ProductRecord(title="同一张卡片", image_path="full-before.jpg")
    covered_next = ProductRecord(title="同一张卡片", image_path="covered-after.jpg")

    Collector._complete_product_observation(existing, covered_next)

    assert existing.image_path == "full-before.jpg"


def test_overlap_keeps_edge_card_that_was_not_previously_collected() -> None:
    repeated = "YAYA鸭鸭同名独立羽绒服"
    collected_tenth = ProductRecord(title="第十件商品", price=155)
    deferred_eleventh = ProductRecord(title=repeated, price=325)
    current_tenth = ProductRecord(title="第十件商品", price=155)
    current_eleventh = ProductRecord(title=repeated, price=325, image_path="eleventh.jpg")
    independent_twelfth = ProductRecord(title=repeated, price=325, image_path="twelfth.jpg")
    following = ProductRecord(title="后续商品", price=105)

    new_products = Collector._products_after_viewport_overlap(
        [collected_tenth],
        [collected_tenth, deferred_eleventh],
        [current_tenth, current_eleventh, independent_twelfth, following],
        overlap=2,
    )

    assert new_products == [current_eleventh, independent_twelfth, following]


def test_product_scroll_accepts_first_valid_changed_hierarchy(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, min_action_interval=0.1)
    settings.ensure_dirs()
    device = FixtureDevice(Path("fixtures/douyin_shop.xml"))
    collector = Collector(settings, device, DataStore(settings.database_path))
    before_path = device.dump_ui(tmp_path / "before.xml")
    before_xml = before_path.read_text(encoding="utf-8")
    device.swipe(500, 1800, 500, 700, 500)
    dump_count = 0
    original_dump = device.dump_ui

    def counted_dump(destination: Path) -> Path:
        nonlocal dump_count
        dump_count += 1
        return original_dump(destination)

    device.dump_ui = counted_dump
    _xml, _texts, signature = collector._wait_for_product_scroll(
        tmp_path / "after.xml", before_xml, timeout=1.0
    )

    assert signature != collector._product_title_signature(before_xml)
    assert dump_count == 1
