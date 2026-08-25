from __future__ import annotations

import os
from pathlib import Path

from wen.config import Settings

from .appium import AppiumDevice
from .base import DeviceBackend


def create_device(settings: Settings) -> DeviceBackend:
    """Create the single supported production device pipeline.

    Appium + UiAutomator2 is the orchestration layer; AppiumDevice internally
    keeps ADB available for screenshots, gestures and UI-dump fallback.
    """
    return AppiumDevice(
        server_url=settings.appium_url,
        serial=settings.device_serial,
        package=settings.douyin_package,
        activity=settings.douyin_activity,
        adb_path=resolve_adb_path(settings),
    )


def resolve_adb_path(settings: Settings) -> str:
    """Return the ADB executable used by both Appium and emergency stop."""
    if settings.adb_path:
        return settings.adb_path
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        candidate = Path(android_home) / "platform-tools" / "adb"
        if candidate.exists():
            return str(candidate)
    candidate = Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"
    return str(candidate) if candidate.exists() else "adb"
