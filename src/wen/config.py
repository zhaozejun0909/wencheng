from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEN_", env_file=".env", extra="ignore")

    data_dir: Path = Field(default=Path("data"))
    device_serial: str | None = None
    adb_path: str | None = None
    appium_url: str = "http://127.0.0.1:4723"
    # 正式抖音 Android 包名；部分测试包/模拟器可通过 WEN_DOUYIN_PACKAGE 覆盖。
    douyin_package: str = "com.ss.android.ugc.aweme"
    douyin_activity: str = "com.ss.android.ugc.aweme.splash.SplashActivity"
    max_products: int = Field(default=20, ge=1, le=500)
    min_action_interval: float = Field(default=1.2, ge=0.1, le=60)
    # 商品目录精准读取需要覆盖较长的店铺列表；范围查询达到数量后会提前停止。
    max_scrolls: int = Field(default=50, ge=0, le=100)
    ocr_provider: str = "none"
    llm_provider: str = "disabled"
    llm_api_key: str | None = None

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "wen.sqlite3"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
