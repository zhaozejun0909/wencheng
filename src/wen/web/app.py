from __future__ import annotations

import json
import logging
import multiprocessing
import re
import shutil
import signal
import threading
from collections.abc import Callable
from datetime import datetime
from html import escape
from mimetypes import guess_type
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from wen.config import Settings
from wen.device import DeviceBackend
from wen.device.adb import AdbDevice
from wen.device.factory import create_device, resolve_adb_path
from wen.extract import create_ocr_provider
from wen.models import (
    CollectionResult,
    JobStatus,
    PreciseQueryMode,
    ProductSelection,
    ProductSelectionMode,
    ProductSortMode,
    QueryGroup,
    QueryPlan,
    StoreLocatorMode,
)
from wen.storage import DataStore, format_china_time
from wen.workflows.collector import Collector

logger = logging.getLogger(__name__)


class CollectRequest(BaseModel):
    # keyword 为旧接口兼容字段；收藏方式使用固定范围“我的收藏”，店铺方式填写完整店铺名。
    keyword: str = Field(default="", max_length=100)
    store_name: str | None = Field(default=None, max_length=100)
    store_locator_mode: StoreLocatorMode = StoreLocatorMode.NAME
    sec_shop_id: str | None = Field(default=None, max_length=160)
    max_products: int = Field(default=20, ge=1, le=500)
    product_titles: list[str] = Field(default_factory=list, max_length=500)
    # Precise-query references introduced after title-only matching.  The
    # legacy product_titles field remains accepted for old clients.
    product_selections: list[ProductSelection] = Field(default_factory=list, max_length=500)
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    selection_mode: ProductSelectionMode = ProductSelectionMode.RANGE
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE
    product_ids: list[str] = Field(default_factory=list, max_length=500)
    sort_mode: ProductSortMode = ProductSortMode.COMPREHENSIVE
    query_group_id: str | None = None
    query_group_name: str | None = Field(default=None, max_length=100)
    query_run_id: str | None = Field(default=None, max_length=100)


class QueryGroupRequest(BaseModel):
    id: str | None = None
    name: str | None = Field(default=None, max_length=100)
    # 收藏模式不绑定店铺；店铺商品查询绑定店铺；精准查询只绑定商品 ID。
    store_name: str | None = Field(default=None, max_length=100)
    store_locator_mode: StoreLocatorMode = StoreLocatorMode.NAME
    sec_shop_id: str | None = Field(default=None, max_length=160)
    selection_mode: ProductSelectionMode = ProductSelectionMode.RANGE
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE
    sort_mode: ProductSortMode = ProductSortMode.COMPREHENSIVE
    limit_count: int = Field(default=10, ge=1, le=500)
    selected_product_titles: list[str] = Field(default_factory=list, max_length=500)
    selected_products: list[ProductSelection] = Field(default_factory=list, max_length=500)
    product_ids: list[str] = Field(default_factory=list, max_length=500)
    schedule_enabled: bool = False
    schedule_cron: str | None = Field(default=None, max_length=100)


class QueryGroupRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class QueryPlanRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_ids: list[str] = Field(default_factory=list, max_length=500)
    schedule_enabled: bool = False
    schedule_cron: str | None = Field(default=None, max_length=100)


class ProductIdExtractRequest(BaseModel):
    share_text: str = Field(min_length=1, max_length=20000)


_SHARE_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PRODUCT_ID_PATTERN = re.compile(
    r"(?:product_id|promotion_id)(?:%3[dD]|=)(\d{6,30})",
    re.IGNORECASE,
)


def _is_supported_douyin_share_host(hostname: str | None) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return (
        host == "douyin.com"
        or host == "jinritemai.com"
        or host.endswith((".douyin.com", ".jinritemai.com"))
    )


def _extract_product_id_from_url(url: str) -> str | None:
    decoded = url
    for _ in range(4):
        match = _PRODUCT_ID_PATTERN.search(decoded)
        if match:
            return match.group(1)
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded

    query = parse_qs(urlsplit(decoded).query)
    for key in ("product_id", "promotion_id", "id"):
        for value in query.get(key, []):
            if re.fullmatch(r"\d{6,30}", value):
                return value
    return None


def _resolve_product_share_id(share_text: str) -> str:
    match = _SHARE_URL_PATTERN.search(share_text)
    if not match:
        raise ValueError("没有识别到抖音商品链接，请粘贴分享链接或完整分享内容。")
    share_url = match.group(0).rstrip(").,，。；;】]}")
    if not _is_supported_douyin_share_host(urlsplit(share_url).hostname):
        raise ValueError("仅支持抖音商品分享链接。")

    direct_id = _extract_product_id_from_url(share_url)
    if direct_id:
        return direct_id

    request = Request(
        share_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
            )
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            resolved_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("分享链接解析失败，请检查链接是否有效或稍后重试。") from exc

    if not _is_supported_douyin_share_host(urlsplit(resolved_url).hostname):
        raise RuntimeError("分享链接跳转到了无法识别的页面。")
    product_id = _extract_product_id_from_url(resolved_url)
    if not product_id:
        raise ValueError("已打开分享链接，但没有提取到商品 ID。")
    return product_id


class _WorkerStop(BaseException):
    """Internal signal used to unwind a query worker without saving a result."""


def _raise_worker_stop(_signum, _frame) -> None:
    raise _WorkerStop()


def _run_collection(
    settings: Settings,
    request: CollectRequest,
    job_id: str,
    device: DeviceBackend,
) -> CollectionResult:
    product_id_mode = request.selection_mode == ProductSelectionMode.PRECISE
    keyword = (
        "我的收藏"
        if request.selection_mode == ProductSelectionMode.FAVORITES
        else "商品 ID 查询"
        if product_id_mode
        else request.sec_shop_id or request.store_name or request.keyword
    ).strip()
    return Collector(
        settings,
        device,
        DataStore(settings.database_path),
        ocr=create_ocr_provider(settings.ocr_provider),
    ).run(
        keyword,
        request.max_products,
        job_id=job_id,
        store_name=request.store_name,
        product_titles=request.product_titles,
        product_selections=request.product_selections,
        price_min=request.price_min,
        price_max=request.price_max,
        selection_mode=request.selection_mode,
        sort_mode=request.sort_mode,
        query_group_id=request.query_group_id,
        query_group_name=request.query_group_name,
        query_run_id=request.query_run_id,
        precise_query_mode=(
            PreciseQueryMode.PRODUCT_IDS
            if product_id_mode
            else request.precise_query_mode
        ),
        store_locator_mode=request.store_locator_mode,
        sec_shop_id=request.sec_shop_id,
        product_ids=request.product_ids,
    )


def _collection_process_main(
    settings_values: dict[str, object],
    request_values: dict[str, object],
    job_id: str,
) -> None:
    """Run one production query in a killable macOS child process."""
    signal.signal(signal.SIGTERM, _raise_worker_stop)
    settings = Settings.model_validate(settings_values)
    request = CollectRequest.model_validate(request_values)
    device: DeviceBackend | None = None
    try:
        device = create_device(settings)
        _run_collection(settings, request, job_id, device)
    except _WorkerStop:
        # Collector.run owns normal cleanup.  This extra close covers the
        # narrow window between device creation and entering Collector.run.
        if device is not None:
            try:
                device.close()
            except Exception as exc:  # noqa: BLE001 - the parent has a hard-kill fallback
                logger.debug("closing stopped collection device failed: %s", exc)


def _terminate_process(
    process: multiprocessing.Process,
    grace_seconds: float = 0.8,
    after_terminate: Callable[[], None] | None = None,
) -> None:
    """Terminate gracefully first, then hard-kill a stuck worker."""
    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    if after_terminate is not None:
        after_terminate()
    process.join(timeout=grace_seconds)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.5)


def _force_stop_douyin(settings: Settings) -> None:
    """Stop Douyin through ADB without waiting behind an Appium command."""
    try:
        AdbDevice(
            serial=settings.device_serial,
            adb_path=resolve_adb_path(settings),
        ).stop_app(settings.douyin_package)
    except Exception as exc:  # noqa: BLE001 - killing the worker remains authoritative
        logger.debug("direct ADB force-stop failed: %s", exc)


def create_app(
    settings: Settings | None = None,
    device_factory: Callable[[Settings], DeviceBackend] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    make_device = device_factory or create_device
    process_context = multiprocessing.get_context("spawn")
    store = DataStore(settings.database_path)
    app = FastAPI(title="文成数据监查", version="0.1.0")
    running: dict[str, CollectionResult] = {}
    active_devices: dict[str, DeviceBackend] = {}
    active_processes: dict[str, multiprocessing.Process] = {}
    stop_requested: set[str] = set()
    lock = threading.Lock()

    def discard_stopped_job(
        job_id: str,
        evidence_dir: str | None = None,
        *,
        clear_stop: bool = True,
    ) -> None:
        """Delete a stopped job after its worker has released the device.

        Evidence is constrained to one direct child of the configured evidence
        root whose name starts with this job ID.  This keeps cleanup explicit and
        prevents a malformed result path from broadening the deletion scope.
        """
        if evidence_dir is None:
            stored = store.get_result(job_id)
            evidence_dir = stored.evidence_dir if stored else None
        store.delete_job(job_id)
        evidence_root = settings.evidence_dir.resolve()
        candidates: list[Path] = []
        if evidence_dir:
            candidates.append(Path(evidence_dir))
        candidates.extend(settings.evidence_dir.glob(f"{job_id}_*"))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if (
                resolved.is_dir()
                and resolved.parent == evidence_root
                and resolved.name.startswith(f"{job_id}_")
            ):
                try:
                    shutil.rmtree(resolved)
                except FileNotFoundError:
                    pass
        with lock:
            running.pop(job_id, None)
            active_devices.pop(job_id, None)
            active_processes.pop(job_id, None)
            if clear_stop:
                stop_requested.discard(job_id)

    def execute_inline(request: CollectRequest, job_id: str) -> None:
        """Keep injected fixture devices in-process for deterministic tests."""
        device = make_device(settings)
        with lock:
            active_devices[job_id] = device
            was_stopped_before_start = job_id in stop_requested
        if was_stopped_before_start:
            try:
                device.stop_app(settings.douyin_package)
            finally:
                device.close()
                discard_stopped_job(job_id)
            return
        result: CollectionResult | None = None
        try:
            result = _run_collection(settings, request, job_id, device)
        finally:
            with lock:
                was_stopped = job_id in stop_requested
                if not was_stopped:
                    active_devices.pop(job_id, None)
            if was_stopped:
                discard_stopped_job(job_id, result.evidence_dir if result else None)
            elif result is not None:
                with lock:
                    running[job_id] = result

    def execute_process(request: CollectRequest, job_id: str) -> None:
        """Start and supervise one production query child process."""
        with lock:
            was_stopped_before_start = job_id in stop_requested
        if was_stopped_before_start:
            discard_stopped_job(job_id)
            return

        process = process_context.Process(
            target=_collection_process_main,
            args=(
                settings.model_dump(mode="json"),
                request.model_dump(mode="json"),
                job_id,
            ),
            name=f"wen-query-{job_id}",
        )
        try:
            process.start()
        except Exception as exc:  # noqa: BLE001 - persist a useful worker-start error
            with lock:
                was_stopped = job_id in stop_requested
            if was_stopped:
                discard_stopped_job(job_id)
                return
            failed = store.get_job_status(job_id)
            if failed is not None:
                failed.status = JobStatus.FAILED
                failed.state = "failed"
                failed.finished_at = datetime.now().astimezone()
                failed.errors.append(f"无法启动查询进程：{exc}")
                store.save_result(failed)
            return

        with lock:
            active_processes[job_id] = process
            was_stopped = job_id in stop_requested
        if was_stopped:
            _terminate_process(process, after_terminate=lambda: _force_stop_douyin(settings))
        else:
            process.join()

        with lock:
            was_stopped = job_id in stop_requested
            if not was_stopped:
                active_processes.pop(job_id, None)
        if was_stopped:
            discard_stopped_job(job_id)
            return

        result = store.get_result(job_id)
        if process.exitcode not in (0, None) and (
            result is None or result.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        ):
            result = result or store.get_job_status(job_id)
            if result is not None:
                result.status = JobStatus.FAILED
                result.state = "failed"
                result.finished_at = datetime.now().astimezone()
                result.errors.append(f"查询进程异常退出（代码 {process.exitcode}）。")
                store.save_result(result)
        if result is not None:
            with lock:
                running[job_id] = result

    def shutdown_workers() -> None:
        with lock:
            processes = list(active_processes.values())
        for process in processes:
            _terminate_process(process, after_terminate=lambda: _force_stop_douyin(settings))

    app.router.add_event_handler("shutdown", shutdown_workers)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = Path(__file__).with_name("index.html")
        content = page.read_text(encoding="utf-8")
        initial_groups = json.dumps(
            [_query_group_view(group) for group in store.list_query_groups()],
            ensure_ascii=False,
        ).replace("</", "<\\/")
        marker = '<script id="initialGroups" type="application/json"></script>'
        return content.replace(
            marker,
            f'<script id="initialGroups" type="application/json">{initial_groups}</script>',
            1,
        )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "version": app.version, "data_dir": str(settings.data_dir)}

    @app.post("/api/product-id/extract")
    def extract_product_id(request: ProductIdExtractRequest) -> dict[str, str]:
        try:
            return {"product_id": _resolve_product_share_id(request.share_text)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/jobs")
    def jobs(page: int = 1, page_size: int = 5) -> dict[str, object]:
        page = max(1, page)
        # The history view intentionally stays compact: five records per page.
        page_size = min(5, max(1, page_size))
        total = store.count_jobs()
        return {
            "items": store.list_jobs(limit=page_size, offset=(page - 1) * page_size),
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
        }

    @app.get("/api/query-runs")
    def query_runs(page: int = 1, page_size: int = 5) -> dict[str, object]:
        page = max(1, page)
        page_size = min(5, max(1, page_size))
        total = store.count_query_runs()
        return {
            "items": store.list_query_runs(limit=page_size, offset=(page - 1) * page_size),
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
        }

    @app.delete("/api/query-runs/{run_id}")
    def delete_query_run(run_id: str) -> dict[str, object]:
        jobs = store.query_run_jobs(run_id)
        if not jobs:
            raise HTTPException(status_code=404, detail="查询记录不存在或已被删除")
        job_ids = [job["id"] for job in jobs]
        with lock:
            active = any(
                job_id in active_devices or job_id in active_processes
                for job_id in job_ids
            )
        unfinished = any(
            job["status"] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
            for job in jobs
        )
        if active or unfinished:
            raise HTTPException(status_code=409, detail="查询仍在进行，结束后才能删除记录")
        for job_id in job_ids:
            discard_stopped_job(job_id)
        return {"ok": True, "deleted_jobs": len(job_ids)}

    @app.post("/api/collect")
    def collect(request: CollectRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
        is_favorites = request.selection_mode == ProductSelectionMode.FAVORITES
        product_id_mode = request.selection_mode == ProductSelectionMode.PRECISE
        precise_query_mode = (
            PreciseQueryMode.PRODUCT_IDS
            if product_id_mode
            else request.precise_query_mode
        )
        store_name = (
            request.store_name
            or (
                request.keyword
                if not is_favorites
                and not product_id_mode
                and request.store_locator_mode == StoreLocatorMode.NAME
                else ""
            )
        ).strip() or None
        sec_shop_id = (request.sec_shop_id or "").strip() or None
        product_ids = _normalize_product_ids(request.product_ids)
        if product_id_mode:
            store_name = None
            sec_shop_id = None
        _validate_query_scope(
            store_name,
            request.selection_mode,
            precise_query_mode,
            request.store_locator_mode,
            sec_shop_id,
            product_ids,
        )
        keyword = (
            "我的收藏"
            if is_favorites
            else "商品 ID 查询"
            if product_id_mode
            else sec_shop_id
            if request.store_locator_mode == StoreLocatorMode.SEC_SHOP_ID
            else (store_name or request.keyword).strip()
        )
        request = request.model_copy(
            update={
                "store_name": store_name,
                "sec_shop_id": sec_shop_id,
                "product_ids": product_ids,
                "precise_query_mode": precise_query_mode,
            }
        )
        job_id = uuid4().hex[:12]
        queued = CollectionResult(
            job_id=job_id,
            status=JobStatus.QUEUED,
            backend="appium",
            keyword=keyword,
            requested_store_name=(
                store_name
                if not is_favorites and not product_id_mode
                and request.store_locator_mode == StoreLocatorMode.NAME
                else None
            ),
            requested_sec_shop_id=(
                sec_shop_id
                if not is_favorites and not product_id_mode
                and request.store_locator_mode == StoreLocatorMode.SEC_SHOP_ID
                else None
            ),
            precise_query_mode=request.precise_query_mode,
            query_run_id=request.query_run_id,
            query_group_id=request.query_group_id,
            query_group_name=request.query_group_name,
            selection_mode=request.selection_mode,
            sort_mode=request.sort_mode,
            state="queued",
        )
        store.create_job(queued)
        background_tasks.add_task(
            execute_inline if device_factory is not None else execute_process,
            request,
            job_id,
        )
        return {"job_id": job_id, "status": JobStatus.QUEUED.value}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> JSONResponse:
        result = store.get_result(job_id) or store.get_job_status(job_id)
        if result:
            return JSONResponse(result.model_dump(mode="json"))
        with lock:
            if job_id in running:
                return JSONResponse(running[job_id].model_dump(mode="json"))
        raise HTTPException(status_code=404, detail="任务不存在或尚未开始")

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict[str, object]:
        """Hard-stop an active query process and immediately delete its artifacts."""
        with lock:
            active = job_id in active_devices or job_id in active_processes
        status = store.get_job_status(job_id)
        if status is None and not active:
            raise HTTPException(status_code=404, detail="任务不存在或已被删除")
        if not active and status and status.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise HTTPException(status_code=409, detail="任务已经结束，无法停止。")
        with lock:
            stop_requested.add(job_id)
            device = active_devices.get(job_id)
            process = active_processes.get(job_id)
        if process is not None:
            # SIGTERM lets Collector finally close its Appium session.  ADB is
            # issued independently so it cannot queue behind a blocked Appium
            # command; a hard kill follows if graceful unwind exceeds 0.8s.
            _terminate_process(
                process,
                after_terminate=lambda: _force_stop_douyin(settings),
            )
        elif device is not None:
            # Injected fixture devices stay in-process for tests. Production
            # queries never use this cooperative fallback.
            try:
                device.stop_app(settings.douyin_package)
            except Exception as exc:  # noqa: BLE001 - cleanup continues even if device disconnected
                logger.debug("stopping device for job %s failed: %s", job_id, exc)
        # Keep the stop tombstone until the supervisor has observed process
        # exit, but remove all user-visible data before returning to the page.
        discard_stopped_job(job_id, clear_stop=False)
        return {"ok": True, "status": "stopped", "job_id": job_id}

    @app.get("/api/query-groups")
    def query_groups() -> list[dict[str, object]]:
        return [_query_group_view(group) for group in store.list_query_groups()]

    @app.post("/api/query-groups")
    def create_query_group(request: QueryGroupRequest) -> dict[str, object]:
        now = datetime.now().astimezone()
        store_name = (request.store_name or "").strip() or None
        sec_shop_id = (request.sec_shop_id or "").strip() or None
        product_ids = _normalize_product_ids(request.product_ids)
        precise_query_mode = (
            PreciseQueryMode.PRODUCT_IDS
            if request.selection_mode == ProductSelectionMode.PRECISE
            else request.precise_query_mode
        )
        if request.selection_mode == ProductSelectionMode.PRECISE:
            store_name = None
            sec_shop_id = None
        _validate_query_scope(
            store_name, request.selection_mode, precise_query_mode,
            request.store_locator_mode, sec_shop_id, product_ids,
        )
        name = (request.name or "").strip() or _default_query_group_name(
            store_name, request.selection_mode, precise_query_mode,
            request.store_locator_mode, sec_shop_id,
        )
        group = QueryGroup(
            id=request.id or uuid4().hex[:12],
            name=name,
            store_name=store_name,
            store_locator_mode=request.store_locator_mode,
            sec_shop_id=sec_shop_id,
            selection_mode=request.selection_mode,
            precise_query_mode=precise_query_mode,
            sort_mode=request.sort_mode,
            limit_count=request.limit_count,
            selected_product_titles=request.selected_product_titles,
            selected_products=request.selected_products,
            product_ids=product_ids,
            schedule_enabled=request.schedule_enabled,
            schedule_cron=request.schedule_cron,
            created_at=now,
            updated_at=now,
        )
        store.save_query_group(group)
        return group.model_dump(mode="json")

    @app.put("/api/query-groups/{group_id}")
    def update_query_group(group_id: str, request: QueryGroupRequest) -> dict[str, object]:
        existing = store.get_query_group(group_id)
        if not existing:
            raise HTTPException(status_code=404, detail="查询组不存在")
        store_name = (request.store_name or "").strip() or None
        sec_shop_id = (request.sec_shop_id or "").strip() or None
        product_ids = _normalize_product_ids(request.product_ids)
        precise_query_mode = (
            PreciseQueryMode.PRODUCT_IDS
            if request.selection_mode == ProductSelectionMode.PRECISE
            else request.precise_query_mode
        )
        if request.selection_mode == ProductSelectionMode.PRECISE:
            store_name = None
            sec_shop_id = None
        _validate_query_scope(
            store_name, request.selection_mode, precise_query_mode,
            request.store_locator_mode, sec_shop_id, product_ids,
        )
        name = (request.name or "").strip() or existing.name or _default_query_group_name(
            store_name, request.selection_mode, precise_query_mode,
            request.store_locator_mode, sec_shop_id,
        )
        group = QueryGroup(
            id=group_id,
            name=name,
            store_name=store_name,
            store_locator_mode=request.store_locator_mode,
            sec_shop_id=sec_shop_id,
            selection_mode=request.selection_mode,
            precise_query_mode=precise_query_mode,
            sort_mode=request.sort_mode,
            limit_count=request.limit_count,
            selected_product_titles=request.selected_product_titles,
            selected_products=request.selected_products,
            product_ids=product_ids,
            schedule_enabled=request.schedule_enabled,
            schedule_cron=request.schedule_cron,
            created_at=existing.created_at,
            updated_at=datetime.now().astimezone(),
        )
        store.save_query_group(group)
        return group.model_dump(mode="json")

    @app.patch("/api/query-groups/{group_id}/name")
    def rename_query_group(group_id: str, request: QueryGroupRenameRequest) -> dict[str, object]:
        existing = store.get_query_group(group_id)
        if not existing:
            raise HTTPException(status_code=404, detail="查询组不存在")
        renamed = existing.model_copy(
            update={"name": request.name.strip(), "updated_at": datetime.now().astimezone()}
        )
        store.save_query_group(renamed)
        return renamed.model_dump(mode="json")

    @app.delete("/api/query-groups/{group_id}")
    def delete_query_group(group_id: str) -> dict[str, bool]:
        if not store.delete_query_group(group_id):
            raise HTTPException(status_code=404, detail="查询组不存在")
        return {"ok": True}

    @app.get("/api/query-plans")
    def query_plans() -> list[dict[str, object]]:
        return [plan.model_dump(mode="json") for plan in store.list_query_plans()]

    @app.post("/api/query-plans")
    def create_query_plan(request: QueryPlanRequest) -> dict[str, object]:
        now = datetime.now().astimezone()
        missing = [group_id for group_id in request.group_ids if not store.get_query_group(group_id)]
        if missing:
            raise HTTPException(status_code=422, detail=f"查询组不存在：{', '.join(missing)}")
        plan = QueryPlan(
            id=uuid4().hex[:12],
            name=request.name.strip(),
            group_ids=request.group_ids,
            schedule_enabled=request.schedule_enabled,
            schedule_cron=request.schedule_cron,
            created_at=now,
            updated_at=now,
        )
        store.save_query_plan(plan)
        return plan.model_dump(mode="json")

    @app.get("/api/query-plans/{plan_id}")
    def query_plan(plan_id: str) -> dict[str, object]:
        plan = store.get_query_plan(plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="查询不存在")
        return plan.model_dump(mode="json")

    @app.get("/api/jobs/{job_id}/csv")
    def csv_export(job_id: str) -> FileResponse:
        destination = settings.data_dir / "exports" / f"{job_id}.csv"
        try:
            store.export_csv(job_id, destination)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(destination, filename=f"wen-{job_id}.csv", media_type="text/csv")

    @app.get("/api/jobs/{job_id}/xlsx")
    def xlsx_export(job_id: str) -> FileResponse:
        filename = _daily_export_filename()
        destination = settings.data_dir / "exports" / filename
        try:
            store.export_xlsx(job_id, destination)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            destination,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/jobs/{job_id}/image/{image_name:path}")
    def product_image(job_id: str, image_name: str) -> FileResponse:
        result = store.get_result(job_id)
        if result is None or not result.evidence_dir:
            raise HTTPException(status_code=404, detail="任务或商品图片不存在")
        root = Path(result.evidence_dir).resolve()
        requested = Path(image_name)
        # 图片只允许访问当前任务 evidence 根目录下的单个裁剪文件，避免路径穿越。
        if len(requested.parts) != 1 or requested.name != image_name:
            raise HTTPException(status_code=404, detail="商品图片路径无效")
        image_path = (root / requested.name).resolve()
        if image_path.parent != root or not image_path.is_file():
            raise HTTPException(status_code=404, detail="商品图片不存在")
        return FileResponse(image_path, media_type=guess_type(image_path.name)[0] or "image/jpeg")

    @app.get("/api/exports/xlsx")
    def xlsx_batch_export(job_ids: str = "") -> FileResponse:
        ids = [value.strip() for value in job_ids.split(",") if value.strip()]
        if not ids:
            raise HTTPException(status_code=422, detail="至少需要一个查询任务 ID。")
        filename = _daily_export_filename()
        destination = settings.data_dir / "exports" / filename
        try:
            store.export_xlsx_batch(ids, destination)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            destination,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/exports/view", response_class=HTMLResponse)
    def online_results_view(job_ids: str = "") -> HTMLResponse:
        ids = [value.strip() for value in job_ids.split(",") if value.strip()]
        if not ids:
            raise HTTPException(status_code=422, detail="至少需要一个查询任务 ID。")
        try:
            results = store.get_results(ids)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return HTMLResponse(_render_results_html(results))

    return app


def _daily_export_filename() -> str:
    return f"wen-dy-{datetime.now().astimezone():%Y%m%d}.xlsx"


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
        raise HTTPException(
            status_code=422,
            detail="商品 ID 只能包含 6–30 位数字，以下内容无效：" + "、".join(invalid),
        )
    return normalized


def _validate_query_scope(
    store_name: str | None,
    selection_mode: ProductSelectionMode,
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE,
    store_locator_mode: StoreLocatorMode = StoreLocatorMode.NAME,
    sec_shop_id: str | None = None,
    product_ids: list[str] | None = None,
) -> None:
    if selection_mode == ProductSelectionMode.FAVORITES:
        if store_name or sec_shop_id or product_ids:
            raise HTTPException(status_code=422, detail="收藏商品查询不需要填写店铺或商品 ID。")
        return
    if selection_mode == ProductSelectionMode.PRECISE:
        if not product_ids:
            raise HTTPException(status_code=422, detail="精准查询至少要填写一个商品 ID。")
        return
    if store_locator_mode == StoreLocatorMode.SEC_SHOP_ID:
        if not sec_shop_id:
            raise HTTPException(status_code=422, detail="请选择 sec_shop_id 后填写店铺 sec_shop_id。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,160}", sec_shop_id):
            raise HTTPException(status_code=422, detail="sec_shop_id 格式无效，请检查后重新填写。")
        return
    if not store_name:
        raise HTTPException(status_code=422, detail="请选择店铺名称后填写完整店铺名称。")


def _default_query_group_name(
    store_name: str | None,
    selection_mode: ProductSelectionMode,
    precise_query_mode: PreciseQueryMode = PreciseQueryMode.STORE,
    store_locator_mode: StoreLocatorMode = StoreLocatorMode.NAME,
    sec_shop_id: str | None = None,
) -> str:
    if selection_mode == ProductSelectionMode.FAVORITES:
        return "查询我收藏的商品"
    if selection_mode == ProductSelectionMode.PRECISE:
        return "精准查询·商品 ID"
    store_label = sec_shop_id if store_locator_mode == StoreLocatorMode.SEC_SHOP_ID else store_name
    return f"查询店铺内的商品 · {store_label}"


def _query_group_view(group: QueryGroup) -> dict[str, object]:
    """Show legacy automatic names in the current concise format."""
    data = group.model_dump(mode="json")
    store_label = (
        group.sec_shop_id
        if group.store_locator_mode == StoreLocatorMode.SEC_SHOP_ID
        else group.store_name
    )
    legacy_defaults = {
        "我的收藏 · 收藏商品",
        "商品 ID · 精准查询",
        f"{store_label} · 查询店铺内的商品",
    }
    # Exact old defaults are safe to normalize; user-renamed conditions remain
    # untouched. Editing one later persists the displayed name naturally.
    if group.name in legacy_defaults:
        data["name"] = _default_query_group_name(
            group.store_name,
            group.selection_mode,
            group.precise_query_mode,
            group.store_locator_mode,
            group.sec_shop_id,
        )
    return data


def _result_scope_name(result: CollectionResult) -> str:
    if result.selection_mode == ProductSelectionMode.FAVORITES:
        return "我的收藏"
    if result.selection_mode == ProductSelectionMode.PRECISE:
        return "商品 ID 查询"
    return (
        result.store.name
        if result.store and result.store.name
        else (result.requested_store_name or result.requested_sec_shop_id or result.keyword)
    )


def _render_results_html(results: list[CollectionResult]) -> str:
    """Render list-first online results with an optional full-detail view."""
    list_sections: list[str] = []
    detail_sections: list[str] = []
    export_job_ids = ",".join(quote(result.job_id, safe="") for result in results)
    export_url = f"/api/exports/xlsx?job_ids={export_job_ids}"
    export_link = (
        f'<a class="excel-download" href="{escape(export_url)}">下载 Excel</a>'
    )
    for result in results:
        store_name = _result_scope_name(result)
        condition_name = result.query_group_name or "未命名查询条件"
        diagnostics = [f"错误：{value}" for value in result.errors]
        diagnostics.extend(f"提示：{value}" for value in result.warnings)
        if result.missing_product_titles:
            diagnostics.append(
                "精准未命中商品（可能已改名或下架）："
                + "、".join(result.missing_product_titles)
            )
        if result.invalid_favorite_titles:
            diagnostics.append(
                "收藏失效商品（已自动排除）：" + "、".join(result.invalid_favorite_titles)
            )
        if result.failed_product_ids:
            diagnostics.append("商品 ID 读取失败：" + "、".join(result.failed_product_ids))
        diagnostic_html = "".join(
            f'<p class="warning">{escape(value)}</p>' for value in diagnostics
        )
        collected_at = format_china_time(result.finished_at) or "—"
        list_rows = []
        for product in result.products:
            image_url = (
                f"/api/jobs/{quote(result.job_id, safe='')}/image/{quote(product.image_path, safe='')}"
                if product.image_path
                else ""
            )
            image_html = (
                f'<img class="table-image" src="{escape(image_url)}" alt="商品主图" />'
                if image_url
                else '<span class="muted">—</span>'
            )
            price_html = (
                f'<strong class="price-value">{escape(str(product.price))}</strong>'
                if product.price is not None
                else '<span class="muted">—</span>'
            )
            sales = product.displayed_sales_raw or product.displayed_sales or "—"
            list_rows.append(
                f"<tr><td>{image_html}</td><td>{escape(product.title)}</td>"
                f"<td>{price_html}</td><td>{escape(str(sales))}</td></tr>"
            )
        list_table = (
            '<div class="table-scroll"><table class="product-list"><thead><tr>'
            "<th>图片</th><th>商品名称</th><th>价格</th><th>销量</th>"
            "</tr></thead><tbody>"
            + "".join(list_rows)
            + "</tbody></table></div>"
            if list_rows
            else '<p class="muted">没有符合条件的商品。</p>'
        )
        run_rows = []
        run_data = {
            "job_id": result.job_id,
            "keyword": result.keyword,
            "requested_store_name": result.requested_store_name,
            "requested_sec_shop_id": result.requested_sec_shop_id,
            "query_scope": store_name,
            "selection_mode": result.selection_mode.value,
            "precise_query_mode": result.precise_query_mode.value,
            "sort_mode": result.sort_mode.value,
            "evidence_dir": result.evidence_dir,
        }
        for key, value in run_data.items():
            if not _has_display_value(value):
                continue
            run_rows.append(
                f"<tr><th>{escape(_field_label(key))}</th><td>{escape(str(value))}</td></tr>"
            )
        run_table = (
            "<h3>查询详细信息</h3><table class=\"detail-table\">"
            + "".join(run_rows)
            + "</table>"
        )
        store_rows = []
        if result.store:
            for key, value in result.store.model_dump(mode="json").items():
                if key == "fields" or not _has_display_value(value):
                    continue
                store_rows.append(
                    f"<tr><th>{escape(_field_label(key))}</th><td>{escape(str(value))}</td></tr>"
                )
        store_table = (
            "<h3>店铺详细信息</h3><table class=\"detail-table\">"
            + "".join(store_rows)
            + "</table>"
            if store_rows
            else ""
        )
        product_sections = []
        for product in result.products:
            image_url = (
                f"/api/jobs/{quote(result.job_id, safe='')}/image/{quote(product.image_path, safe='')}"
                if product.image_path
                else ""
            )
            detail_rows = []
            for key, value in product.model_dump(mode="json").items():
                if key in {"fields", "image_path"} or not _has_display_value(value):
                    continue
                display_value = escape(str(value))
                if key == "price" and value is not None:
                    display_value = f'<strong class="price-value">{display_value}</strong>'
                detail_rows.append(
                    f"<tr><th>{escape(_field_label(key))}</th><td>{display_value}</td></tr>"
                )
            image_html = (
                f'<img class="detail-image" src="{escape(image_url)}" alt="商品主图" />'
                if image_url
                else '<div class="image-placeholder">未采集到主图</div>'
            )
            product_sections.append(
                f'<article class="product-detail"><div class="image-wrap">{image_html}</div>'
                f"<div class=\"product-info\"><h3>{escape(product.title)}</h3>"
                f"<table class=\"detail-table\">{''.join(detail_rows)}</table></div></article>"
            )
        products_html = "".join(product_sections) or '<p class="muted">没有符合条件的商品。</p>'
        section_header = (
            "<section class=\"result-section\">"
            f"<h2>{escape(store_name)}</h2>"
            f"<div class=\"condition\">查询条件：{escape(condition_name)}</div>"
            f"<div class=\"meta\">状态：{escape(result.status.value)} · 阶段：{escape(result.state)} · 商品数：{len(result.products)} · 采集时间：{escape(collected_at)}</div>"
        )
        list_sections.append(f"{section_header}{diagnostic_html}{list_table}</section>")
        detail_sections.append(
            f"{section_header}{diagnostic_html}{run_table}{store_table}"
            f"<h3>商品详细信息</h3>{products_html}</section>"
        )
    return (
        """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>文成 · 查询结果</title>
<style>
body{max-width:1100px;margin:0 auto;padding:28px 18px 60px;background:#f6f7f9;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
.result-section{background:#fff;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 4px 18px #0000000d;border-left:4px solid #111827}
h2{margin:0 0 4px}h3{margin:18px 0 8px}.condition{font-weight:600;color:#374151;margin-bottom:10px}.meta{color:#6b7280;margin-bottom:12px}.warning{margin:8px 0;padding:10px 12px;border-radius:8px;background:#fef2f2;color:#991b1b;white-space:pre-wrap}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}th{background:#f3f4f6}.price-value{color:#dc2626;font-weight:700}.detail-table th{width:180px}.product-detail{display:flex;gap:18px;padding:14px 0;border-top:1px solid #e5e7eb}.image-wrap{flex:0 0 180px;width:180px;height:180px;background:#f3f4f6;border-radius:10px;overflow:hidden}.detail-image{width:100%;height:100%;object-fit:contain}.image-placeholder{height:100%;display:grid;place-items:center;color:#6b7280;font-size:13px}.product-info{flex:1;min-width:0}.product-info h3{margin-top:0}.muted{color:#6b7280}.view-switch{display:flex;align-items:center;gap:8px;margin:14px 0 4px}.view-switch button,.excel-download{padding:8px 14px;border:1px solid #d1d5db;border-radius:8px;background:#fff;color:#374151;cursor:pointer;font:inherit;text-decoration:none}.view-switch button.active{background:#111827;color:#fff;border-color:#111827}.excel-download{margin-left:auto;background:#111827;color:#fff;border-color:#111827}.table-scroll{overflow-x:auto}.product-list th:first-child{width:74px}.table-image{width:64px;height:64px;object-fit:contain;border-radius:8px;background:#f3f4f6;cursor:zoom-in;transition:transform .18s ease,box-shadow .18s ease;position:relative;z-index:0}.table-image:hover{transform:scale(3);transform-origin:left center;z-index:10;box-shadow:0 8px 24px #0004}
@media(max-width:700px){.product-detail{display:block}.image-wrap{margin-bottom:12px}.detail-table th{width:120px}}
</style></head><body><h1>本次查询结果</h1>
<div class="view-switch" role="group" aria-label="结果展示方式">
<button id="listViewButton" class="active" type="button" onclick="setResultView('list')">列表格式</button>
<button id="detailViewButton" type="button" onclick="setResultView('detail')">详细格式</button>
"""
        + export_link
        + """
</div><main id="listView">"""
        + "".join(list_sections)
        + '</main><main id="detailView" hidden>'
        + "".join(detail_sections)
        + """</main><script>
function setResultView(mode){
  const detail=mode==='detail';
  document.getElementById('listView').hidden=detail;
  document.getElementById('detailView').hidden=!detail;
  document.getElementById('listViewButton').classList.toggle('active',!detail);
  document.getElementById('detailViewButton').classList.toggle('active',detail);
}
</script></body></html>"""
    )


def _field_label(key: str) -> str:
    return {
        "job_id": "任务 ID", "requested_store_name": "请求店铺名称", "requested_sec_shop_id": "请求 sec_shop_id",
        "query_scope": "查询范围", "selection_mode": "商品查询方式", "precise_query_mode": "精准查询方式", "sort_mode": "排序方式", "evidence_dir": "证据目录",
        "keyword": "关键词", "name": "名称", "douyin_id": "抖音 ID", "category": "分类",
        "followers": "粉丝数", "product_count": "商品总数", "description": "描述",
        "title": "商品名称", "price": "当前价格", "original_price": "原价",
        "displayed_sales": "销量（数值）", "displayed_sales_raw": "销量（原文）",
        "rating": "评分", "review_count": "评价数", "product_id": "商品 ID",
        "source_url": "商品链接", "image_path": "主图文件", "position": "列表位置",
        "fields": "扩展字段",
    }.get(key, key)


def _has_display_value(value: object) -> bool:
    """Keep meaningful zero/False values while omitting uncollected fields."""
    return value is not None and value != "" and value != [] and value != {}


def _format_custom_fields(value: object) -> str:
    if not isinstance(value, list):
        return str(value)
    items = []
    for field in value:
        if isinstance(field, dict):
            items.append(f"{field.get('key')}: {field.get('value') or field.get('raw_value') or '—'}")
        else:
            items.append(str(field))
    return "；".join(items) or "—"
