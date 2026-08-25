from __future__ import annotations

import argparse
import sys

from wen.config import Settings
from wen.device.factory import create_device
from wen.extract import create_ocr_provider
from wen.models import ProductSelectionMode, ProductSortMode
from wen.storage import DataStore
from wen.workflows.collector import Collector


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wen", description="文成数据监查 Demo")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="采集一个店铺或我的收藏")
    collect.add_argument("--keyword", default="鸭鸭童装旗舰店")
    collect.add_argument("--store-name", help="店铺页显示的完整名称；不匹配时任务暂停，不会选相似店铺")
    collect.add_argument("--max-products", type=int, default=None)
    collect.add_argument("--product-title", action="append", help="按店铺页商品标题筛选，可重复传入")
    collect.add_argument("--price-min", type=float)
    collect.add_argument("--price-max", type=float)
    collect.add_argument(
        "--selection-mode",
        choices=[mode.value for mode in ProductSelectionMode],
        default=ProductSelectionMode.RANGE.value,
        help="商品查询方式；favorites 读取全部有效收藏，precise_catalog 用于读取店铺目录供用户勾选",
    )
    collect.add_argument(
        "--sort-mode",
        choices=[mode.value for mode in ProductSortMode],
        default=ProductSortMode.COMPREHENSIVE.value,
        help="商品排序：综合、销量、上新或价格方向",
    )
    serve = sub.add_parser("serve", help="启动本地 Web 控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()
    settings.ensure_dirs()
    if args.command == "collect":
        device = create_device(settings)
        result = Collector(
            settings,
            device,
            DataStore(settings.database_path),
            ocr=create_ocr_provider(settings.ocr_provider),
        ).run(
            args.keyword,
            args.max_products,
            store_name=args.store_name,
            product_titles=args.product_title,
            price_min=args.price_min,
            price_max=args.price_max,
            selection_mode=args.selection_mode,
            sort_mode=args.sort_mode,
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.status.value == "succeeded" else 2
    if args.command == "serve":
        import uvicorn

        from wen.web.app import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
