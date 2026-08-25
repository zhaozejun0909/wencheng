from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from wen.models import DeviceInfo


class DeviceError(RuntimeError):
    """设备控制或设备通信错误。"""


class LoginRequired(DeviceError):
    """需要用户在真机上手动完成登录。"""


class RiskControlDetected(DeviceError):
    """检测到验证码、风控或异常访问页面，必须暂停人工处理。"""


class StoreSelectionRequired(DeviceError):
    """搜索结果中无法严格确定目标店铺，需要用户提供完整店铺名。"""

    def __init__(self, target: str, candidates: list[str]) -> None:
        self.target = target
        self.candidates = candidates
        candidate_text = "、".join(candidates[:8]) if candidates else "（未读取到候选店铺）"
        super().__init__(
            f"未能严格命中店铺“{target}”。候选店铺：{candidate_text}。"
            "请使用页面显示的完整店铺名重新运行，不会自动选择相似店铺。"
        )


class DeviceBackend(ABC):
    name = "unknown"
    # Production collectors use the real Android device pipeline. Test-only
    # fixture devices may override this capability without being user-facing
    # backends.
    is_live = True

    @abstractmethod
    def info(self) -> DeviceInfo:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> DeviceInfo:
        raise NotImplementedError

    @abstractmethod
    def start_app(self, package: str, activity: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_app(self, package: str) -> None:
        raise NotImplementedError

    def open_uri(self, uri: str, package: str | None = None) -> None:
        """Open an Android deep link without navigating through the app UI."""
        raise DeviceError(f"设备后端 {self.name} 不支持 URI 直达。")

    @abstractmethod
    def screenshot(self, destination: Path) -> Path:
        raise NotImplementedError

    @abstractmethod
    def dump_ui(self, destination: Path) -> Path:
        raise NotImplementedError

    @abstractmethod
    def tap(self, x: int, y: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        raise NotImplementedError

    def scroll(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        *,
        direction: str = "down",
        percent: float = 0.4,
        speed: int = 1200,
    ) -> bool | None:
        """Scroll a bounded list area and optionally report whether more exists.

        Live Appium devices override this with UiAutomator2's
        ``mobile: scrollGesture``.  The fallback keeps fixture/ADB backends
        usable while the return value remains ``None`` when the backend cannot
        report an end-of-list result.
        """
        if direction.lower() not in {"down", "up"}:
            raise ValueError(f"不支持的滚动方向：{direction}")
        distance = max(1, round(height * percent))
        center_x = left + width // 2
        if direction.lower() == "down":
            start_y = top + height - 24
            end_y = max(top + 24, start_y - distance)
        else:
            start_y = top + 24
            end_y = min(top + height - 24, start_y + distance)
        self.swipe(center_x, start_y, center_x, end_y, max(250, round(distance / max(speed, 1) * 1000)))
        return None

    @abstractmethod
    def type_text(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def back(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """释放后端资源。Appium 会主动关闭会话。"""
        return

    def tap_text(self, text: str) -> bool:
        """按可见文字点击。后端可覆盖；默认由工作流使用 XML 解析。"""
        return False

    def tap_text_exact(self, text: str) -> bool:
        """按完整可见文字点击，避免把复合标签当成目标入口。"""
        return self.tap_text(text)

    def press_enter(self) -> None:
        """提交输入。后端可覆盖。"""
