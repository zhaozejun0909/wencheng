from pathlib import Path

from wen.extract import OcrDetection
from wen.platforms.douyin import DouyinExtractor, parse_count, parse_price


def test_parse_count_units() -> None:
    assert parse_count("1.2万+") == 12000
    assert parse_count("3680") == 3680
    assert parse_count("3千") == 3000
    assert parse_count("2.5亿") == 250000000


def test_parse_price() -> None:
    assert parse_price("¥299") == 299
    assert parse_price("￥79.9") == 79.9


def test_product_detail_uses_purchase_price_not_app_exclusive_price() -> None:
    xml = """
    <hierarchy bounds="[0,0][1280,2772]">
      <node content-desc="图片1" bounds="[0,152][1280,1432]" />
      <node text="YAYA/鸭鸭儿童加厚保暖羽绒服" bounds="[65,1780][1215,1920]" />
      <node text="专享价" bounds="[524,2546][656,2608]" />
      <node text="¥292" bounds="[663,2554][772,2602]" />
      <node text="¥299" bounds="[1003,2554][1113,2602]" />
      <node text="立即购买" bounds="[984,2611][1132,2659]" />
      <node text="加购" bounds="[310,2547][412,2675]" />
    </hierarchy>
    """

    product = DouyinExtractor().extract_product_detail(xml, "3824457047601185029")

    assert product.price == 299
    assert product.fields[0].key == "price"
    assert product.fields[0].raw_value == "立即购买 ¥299"
    exclusive = next(field for field in product.fields if field.key == "app_exclusive_price")
    assert exclusive.value == 292


def test_product_detail_reads_now_order_button_price() -> None:
    xml = """
    <hierarchy bounds="[0,0][1280,2772]">
      <node content-desc="图片1" bounds="[0,152][1280,1858]" />
      <node text="YAYA/鸭鸭高质量童装反季男女中大童羽绒服秋冬加厚儿童羽绒外套" bounds="[65,2197][1215,2351]" />
      <node text="¥359" bounds="[898,2554][1008,2602]" />
      <node text="现在下单" bounds="[795,2611][943,2659]" />
      <node text="后天送达" bounds="[963,2611][1111,2659]" />
    </hierarchy>
    """

    product = DouyinExtractor().extract_product_detail(xml, "3636553152859395915")

    assert product.price == 359
    assert product.fields[0].raw_value == "现在下单 ¥359"


def test_product_detail_ocr_rejects_battery_number_and_app_exclusive_price() -> None:
    xml = """
    <hierarchy bounds="[0,0][1280,2772]">
      <node content-desc="图片1" bounds="[0,152][1280,1432]" />
      <node text="YAYA/鸭鸭儿童加厚保暖羽绒服" bounds="[65,1780][1215,1920]" />
      <node text="专享价" bounds="[524,2546][656,2608]" />
      <node text="加购" bounds="[310,2547][412,2675]" />
    </hierarchy>
    """
    detections = [
        OcrDetection("69", (1093, 67, 1139, 101), 0.99),
        OcrDetection("￥299", (56, 1654, 256, 1739), 0.95),
        OcrDetection("专享价￥292", (522, 2549, 776, 2604), 0.95),
    ]

    product = DouyinExtractor().extract_product_detail(
        xml, "3824457047601185029", detections
    )

    assert product.price == 299
    assert product.fields[0].method == "ocr"


def test_fixture_extracts_store_and_products() -> None:
    xml = Path("fixtures/douyin_shop.xml").read_text(encoding="utf-8")
    store, products = DouyinExtractor().extract(xml, "鸭鸭童装旗舰店")
    assert store.name == "鸭鸭童装旗舰店"
    assert store.followers == 123000
    assert store.product_count == 36
    assert len(products) == 3
    assert products[0].displayed_sales == 12000
    assert products[0].price == 299


def test_product_cards_keep_screen_order_when_titles_have_different_lengths() -> None:
    xml = """
    <hierarchy><node>
      <node text="YAYA/鸭鸭2026儿童春秋新款连帽羽绒服轻薄多色鸭鸭小童保暖外套" bounds="[514,568][1227,702]" />
      <node text="¥109 已享满减" bounds="[514,862][802,923]" />
      <node text="鸭鸭儿童小童羽绒服冬季轻薄款抗寒保暖卡通简约短款时尚" bounds="[514,1055][1227,1189]" />
      <node text="¥109 已享满减" bounds="[514,1349][802,1410]" />
      <node text="鸭鸭儿童羽绒服2025" bounds="[514,1542][1227,1609]" />
      <node text="¥199 已享满减" bounds="[514,1836][800,1897]" />
      <node text="i 羽绒服儿童羽绒服连帽加厚羽绒服" bounds="[514,2114][1227,2248]" />
      <node text="¥269 已享满减" bounds="[514,2323][808,2384]" />
    </node></hierarchy>
    """
    detections = [
        OcrDetection("￥109 已售2000+", (508, 870, 1009, 926), 0.99),
        OcrDetection("￥109 已售50", (510, 1356, 944, 1412), 0.99),
        OcrDetection("￥199 已售1000+", (508, 1841, 1004, 1901), 0.99),
        OcrDetection("￥269 已售10", (507, 2329, 944, 2389), 0.99),
    ]
    _, products = DouyinExtractor().extract(
        xml,
        "鸭鸭童装官方旗舰店",
        [detection.text for detection in detections],
        detections,
    )
    assert [product.title for product in products] == [
        "YAYA/鸭鸭2026儿童春秋新款连帽羽绒服轻薄多色鸭鸭小童保暖外套",
        "鸭鸭儿童小童羽绒服冬季轻薄款抗寒保暖卡通简约短款时尚",
        "鸭鸭儿童羽绒服2025",
        "羽绒服儿童羽绒服连帽加厚羽绒服",
    ]
    assert [product.price for product in products] == [109, 109, 199, 269]
    assert [product.displayed_sales_raw for product in products] == ["2000+", "50", "1000+", "10"]


def test_marketing_badges_are_not_used_as_product_titles() -> None:
    """A badge between title and price must not steal the card's fields."""
    xml = """
    <hierarchy><node>
      <node text="YAYA/鸭鸭貉子真毛领户外羽绒外套儿童连帽中长款加厚保暖羽绒服" bounds="[514,568][1227,702]" />
      <node text="防钻绒好:高密度布少跑绒" bounds="[514,711][908,760]" />
      <node text="¥299 已享满减" bounds="[514,862][808,923]" />
      <node text="YAYA/鸭鸭2025新款中大童时尚洋气冬季外套男童男士女童连帽加厚" bounds="[514,1055][1227,1189]" />
      <node text="达人说：“保暖加厚”" bounds="[514,1198][820,1247]" />
      <node text="¥329 已享满减" bounds="[514,1349][807,1410]" />
      <node text="YAYA/鸭鸭2026儿童春秋新款连帽羽绒服轻薄多色鸭鸭小童保暖外套" bounds="[514,1542][1227,1676]" />
      <node text="直播间同价" bounds="[514,1685][820,1734]" />
      <node text="¥239 已享满减" bounds="[514,1836][807,1897]" />
    </node></hierarchy>
    """
    detections = [
        OcrDetection("￥299已享满减已售7万+", (508, 870, 1009, 926), 0.99),
        OcrDetection("￥329已享满减已售2万+", (508, 1356, 1009, 1412), 0.99),
        OcrDetection("￥239已享满减已售2000+", (508, 1841, 1004, 1901), 0.99),
    ]
    _, products = DouyinExtractor().extract(
        xml,
        "鸭鸭童装官方旗舰店",
        [detection.text for detection in detections],
        detections,
    )
    assert [product.title for product in products] == [
        "YAYA/鸭鸭貉子真毛领户外羽绒外套儿童连帽中长款加厚保暖羽绒服",
        "YAYA/鸭鸭2025新款中大童时尚洋气冬季外套男童男士女童连帽加厚",
        "YAYA/鸭鸭2026儿童春秋新款连帽羽绒服轻薄多色鸭鸭小童保暖外套",
    ]
    assert [product.price for product in products] == [299, 329, 239]
    assert [product.displayed_sales_raw for product in products] == ["7万+", "2万+", "2000+"]


def test_same_title_cards_are_kept_separate_by_card_position_and_price() -> None:
    title = "YAYA/鸭鸭同名羽绒服儿童连帽加厚保暖外套"
    xml = f"""
    <hierarchy><node>
      <node text="{title}" bounds="[514,568][1227,702]" />
      <node text="¥299 已享满减" bounds="[514,862][808,923]" />
      <node text="{title}" bounds="[514,1055][1227,1189]" />
      <node text="¥329 已享满减" bounds="[514,1349][807,1410]" />
    </node></hierarchy>
    """
    detections = [
        OcrDetection("￥299已售7万+", (508, 870, 1009, 926), 0.99),
        OcrDetection("￥329已售2万+", (508, 1356, 1009, 1412), 0.99),
    ]
    _, products = DouyinExtractor().extract(xml, "鸭鸭童装官方旗舰店", ocr_detections=detections)
    assert [(product.title, product.price, product.displayed_sales_raw) for product in products] == [
        (title, 299, "7万+"),
        (title, 329, "2万+"),
    ]


def test_same_title_cards_do_not_move_across_an_intervening_product() -> None:
    repeated = "YAYA/鸭鸭貉子真毛领户外羽绒外套儿童连帽中长款加厚保暖羽绒服"
    different = "运动碎花厚款拉链女男冬季秋冬童装冬装"
    xml = f"""
    <hierarchy><node>
      <node text="{repeated}" bounds="[514,689][1227,823]" />
      <node text="¥329" bounds="[514,980][808,1041]" />
      <node text="{different}" bounds="[514,1176][1227,1310]" />
      <node text="¥159" bounds="[514,1467][808,1528]" />
      <node text="{repeated}" bounds="[514,1663][1227,1797]" />
      <node text="¥329" bounds="[514,1954][808,2015]" />
      <node text="{repeated}" bounds="[514,2150][1227,2284]" />
      <node text="¥329" bounds="[514,2441][808,2502]" />
    </node></hierarchy>
    """
    detections = [
        OcrDetection("已售800+", (820, 980, 1050, 1041), 0.99),
        OcrDetection("已售700+", (820, 1467, 1050, 1528), 0.99),
        OcrDetection("已售500+", (820, 1954, 1050, 2015), 0.99),
        OcrDetection("已售100+", (820, 2441, 1050, 2502), 0.99),
    ]

    _, products = DouyinExtractor().extract(
        xml, "鸭鸭童装官方旗舰店", ocr_detections=detections
    )

    assert [(product.title, product.displayed_sales_raw) for product in products] == [
        (repeated, "800+"),
        (different, "700+"),
        (repeated, "500+"),
        (repeated, "100+"),
    ]


def test_leading_badge_marker_is_removed_but_real_i_names_are_kept() -> None:
    xml = """
    <hierarchy><node>
      <node text="i\u200b \u200b儿童羽绒服男女童加厚保暖冬季新款连帽休闲外套" bounds="[514,500][1227,634]" />
      <node text="¥199" bounds="[514,785][800,846]" />
      <node text="iPhone儿童保暖配件" bounds="[514,950][1227,1017]" />
      <node text="¥99" bounds="[514,1168][800,1229]" />
      <node text="i YAYA/鸭鸭巴恩风儿童羽绒服" bounds="[514,1350][1227,1450]" />
      <node text="¥199" bounds="[514,1550][800,1611]" />
      <node text="v儿童羽绒服男女童加厚保暖外套" bounds="[514,1700][1227,1800]" />
      <node text="¥195" bounds="[514,1900][800,1961]" />
      <node text="vivo儿童定位手表冬季款" bounds="[514,2050][1227,2150]" />
      <node text="¥599" bounds="[514,2250][800,2311]" />
    </node></hierarchy>
    """
    _, products = DouyinExtractor().extract(xml, "鸭鸭童装官方旗舰店")
    assert [product.title for product in products] == [
        "儿童羽绒服男女童加厚保暖冬季新款连帽休闲外套",
        "iPhone儿童保暖配件",
        "YAYA/鸭鸭巴恩风儿童羽绒服",
        "儿童羽绒服男女童加厚保暖外套",
        "vivo儿童定位手表冬季款",
    ]


def test_product_title_containing_same_style_word_is_not_filtered() -> None:
    title = "鸭鸭冬季新款儿童羽绒服大童男女同款加厚"
    xml = f"""
    <hierarchy><node>
      <node text="{title}" bounds="[514,500][1227,634]" />
      <node text="¥239" bounds="[514,785][800,846]" />
    </node></hierarchy>
    """
    _, products = DouyinExtractor().extract(xml, "鸭鸭童装官方旗舰店")
    assert [(product.title, product.price) for product in products] == [(title, 239)]


def test_same_title_cards_with_different_prices_are_not_dropped() -> None:
    xml = """
    <hierarchy><node>
      <node text="YAYA/鸭鸭貉子真毛领户外羽绒外套儿童连帽中长款加厚保暖羽绒服" bounds="[514,500][1227,634]" />
      <node text="¥299" bounds="[514,785][800,846]" />
      <node text="YAYA/鸭鸭貉子真毛领户外羽绒外套儿童连帽中长款加厚保暖羽绒服" bounds="[514,900][1227,1034]" />
      <node text="¥329" bounds="[514,1185][800,1246]" />
    </node></hierarchy>
    """
    detections = [
        OcrDetection("￥299 已售7万+", (508, 790, 1000, 850), 0.99),
        OcrDetection("￥329 已售2万+", (508, 1190, 1000, 1250), 0.99),
    ]
    _, products = DouyinExtractor().extract(
        xml,
        "鸭鸭童装官方旗舰店",
        [detection.text for detection in detections],
        detections,
    )
    assert len(products) == 2
    assert [product.price for product in products] == [299, 329]
    assert [product.displayed_sales_raw for product in products] == ["7万+", "2万+"]


def test_text_only_fallback_keeps_identical_visible_card_occurrences() -> None:
    products = DouyinExtractor._products_from_texts(
        [
            "同名羽绒服商品",
            "¥199",
            "已售10",
            "同名羽绒服商品",
            "¥199",
            "已售10",
        ]
    )
    assert [(product.title, product.price, product.displayed_sales_raw) for product in products] == [
        ("同名羽绒服商品", 199, "10"),
        ("同名羽绒服商品", 199, "10"),
    ]
