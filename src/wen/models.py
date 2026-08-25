from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProductSelectionMode(StrEnum):
    # 收藏是独立于店铺的商品来源；range/precise 仍然绑定店铺。
    FAVORITES = "favorites"
    RANGE = "range"
    PRECISE = "precise"
    PRECISE_CATALOG = "precise_catalog"


class PreciseQueryMode(StrEnum):
    STORE = "store"
    PRODUCT_IDS = "product_ids"


class StoreLocatorMode(StrEnum):
    NAME = "name"
    SEC_SHOP_ID = "sec_shop_id"


class ProductSortMode(StrEnum):
    COMPREHENSIVE = "comprehensive"
    SALES = "sales"
    NEWEST = "newest"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


class FieldValue(BaseModel):
    key: str
    value: Any = None
    raw_value: str | None = None
    method: str = "ui"
    confidence: float = Field(default=1.0, ge=0, le=1)


class ProductRecord(BaseModel):
    title: str
    price: float | None = None
    original_price: float | None = None
    displayed_sales: int | None = None
    displayed_sales_raw: str | None = None
    rating: float | None = None
    review_count: int | None = None
    product_id: str | None = None
    source_url: str | None = None
    # 商品列表截图裁剪出的主图相对路径；实际文件位于任务 evidence 目录。
    image_path: str | None = None
    position: int | None = None
    fields: list[FieldValue] = Field(default_factory=list)


class ProductSelection(BaseModel):
    """A saved precise-query reference.

    The title is the human-visible identity.  Price is stored as a second
    discriminator because two live cards can legitimately have the same
    title.  ``None`` keeps older title-only query groups compatible.
    """

    title: str = Field(min_length=1, max_length=500)
    price: float | None = Field(default=None, ge=0)


class StoreRecord(BaseModel):
    keyword: str
    name: str | None = None
    douyin_id: str | None = None
    category: str | None = None
    followers: int | None = None
    product_count: int | None = None
    description: str | None = None
    fields: list[FieldValue] = Field(default_factory=list)


class CollectionResult(BaseModel):
    job_id: str
    # 一次点击“开始查询”可能包含多个查询条件；用 run_id 将这些任务归并。
    query_run_id: str | None = None
    status: JobStatus
    backend: str
    keyword: str
    requested_store_name: str | None = None
    requested_sec_shop_id: str | None = None
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE
    query_group_id: str | None = None
    query_group_name: str | None = None
    selection_mode: ProductSelectionMode = ProductSelectionMode.RANGE
    sort_mode: ProductSortMode = ProductSortMode.COMPREHENSIVE
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    store: StoreRecord | None = None
    store_candidates: list[str] = Field(default_factory=list)
    products: list[ProductRecord] = Field(default_factory=list)
    # 精准查询中保存的商品引用逐一核对后的未命中项（新版本会带价格）。
    # 该字段会随完整结果持久化，供页面和汇总导出明确提示“商品可能改名或下架”。
    missing_product_titles: list[str] = Field(default_factory=list)
    # 商品 ID 直达失败时保留原始 ID；其他 ID 仍可正常返回。
    failed_product_ids: list[str] = Field(default_factory=list)
    # 收藏页中已失效/下架的条目不会进入 products，但保留标题用于提示与审计。
    invalid_favorite_titles: list[str] = Field(default_factory=list)
    evidence_dir: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    state: str = "created"


class QueryGroup(BaseModel):
    """持久化的商品查询条件。

    店铺模式使用 store_name；favorites 模式的 store_name 为 None，后续定时任务
    可以直接引用同一个 group_id 而不改变查询范围语义。
    """

    id: str
    name: str = "未命名查询条件"
    store_name: str | None = None
    store_locator_mode: StoreLocatorMode = StoreLocatorMode.NAME
    sec_shop_id: str | None = None
    selection_mode: ProductSelectionMode = ProductSelectionMode.RANGE
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE
    sort_mode: ProductSortMode = ProductSortMode.COMPREHENSIVE
    limit_count: int = Field(default=10, ge=1, le=500)
    selected_product_titles: list[str] = Field(default_factory=list, max_length=500)
    # New precise-query identity.  Keep selected_product_titles for persisted
    # groups created by older versions and API clients.
    selected_products: list[ProductSelection] = Field(default_factory=list, max_length=500)
    product_ids: list[str] = Field(default_factory=list, max_length=500)
    schedule_enabled: bool = False
    schedule_cron: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class QueryPlan(BaseModel):
    """一次查询所包含的多个查询组；调度器后续按 group_ids 顺序执行。"""

    id: str
    name: str
    group_ids: list[str] = Field(default_factory=list, max_length=500)
    schedule_enabled: bool = False
    schedule_cron: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DeviceInfo(BaseModel):
    serial: str
    state: str
    model: str | None = None
    android_version: str | None = None
    is_emulator: bool = False
    screen_width: int | None = None
    screen_height: int | None = None
    backend: str = "unknown"
