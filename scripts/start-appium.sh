#!/usr/bin/env bash
set -euo pipefail

# macOS Android Studio 默认安装位置。也可以在运行前显式设置 ANDROID_SDK_ROOT/JAVA_HOME。
WEN_SDK_PATH="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
WEN_JAVA_PATH="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"

if [[ ! -x "$WEN_SDK_PATH/platform-tools/adb" ]]; then
  echo "找不到 adb：$WEN_SDK_PATH/platform-tools/adb" >&2
  echo "请先安装 Android SDK Platform-Tools，或设置 ANDROID_SDK_ROOT。" >&2
  exit 1
fi
if [[ ! -x "$WEN_JAVA_PATH/bin/java" ]]; then
  echo "找不到 Java：$WEN_JAVA_PATH/bin/java" >&2
  echo "请设置 JAVA_HOME 指向 Android Studio 自带 JBR 或其他 JDK 17+。" >&2
  exit 1
fi

export ANDROID_HOME="$WEN_SDK_PATH"
export ANDROID_SDK_ROOT="$WEN_SDK_PATH"
export JAVA_HOME="$WEN_JAVA_PATH"
export PATH="$WEN_JAVA_PATH/bin:$WEN_SDK_PATH/platform-tools:$WEN_SDK_PATH/emulator:$PATH"

# Codex/Terminal 可能使用了另一个 NVM Node 版本，而 Appium 安装在
# 其他版本下。优先使用 PATH，找不到时自动定位已安装的 NVM Appium。
WEN_APPIUM_BIN="$(command -v appium || true)"
if [[ -z "$WEN_APPIUM_BIN" ]]; then
  for WEN_APPIUM_CANDIDATE in "$HOME"/.nvm/versions/node/*/bin/appium; do
    if [[ -x "$WEN_APPIUM_CANDIDATE" ]]; then
      WEN_APPIUM_BIN="$WEN_APPIUM_CANDIDATE"
      break
    fi
  done
fi
if [[ -z "$WEN_APPIUM_BIN" ]]; then
  echo "找不到 Appium：请先执行 npm install -g appium" >&2
  exit 1
fi
export PATH="$(dirname "$WEN_APPIUM_BIN"):$PATH"

echo "Android SDK: $ANDROID_SDK_ROOT"
echo "Java: $JAVA_HOME"
echo "Appium: $WEN_APPIUM_BIN"
echo "设备："
adb devices -l
echo "启动 Appium（Ctrl-C 停止）..."
exec "$WEN_APPIUM_BIN"
