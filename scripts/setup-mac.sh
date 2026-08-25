#!/usr/bin/env bash
set -Eeuo pipefail

# Wen macOS environment bootstrap.
# Installs the local Python/Web stack and the Android/Appium prerequisites.
# It is intentionally idempotent: already installed components are skipped.

WEN_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEN_SDK_PATH="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
WEN_JAVA_PATH="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"

info() { printf '\033[1;34m[wen]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[wen]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[wen] 错误：\033[0m %s\n' "$*" >&2; exit 1; }

trap 'fail "安装过程中出现错误（第 ${LINENO} 行）。请查看上面的命令输出。"' ERR

[[ "$(uname -s)" == "Darwin" ]] || fail "此脚本只支持 macOS。"

if ! xcode-select -p >/dev/null 2>&1; then
  warn "未检测到 macOS 命令行工具。现在会打开系统安装窗口。"
  xcode-select --install >/dev/null 2>&1 || true
  fail "请完成命令行工具安装后，再重新运行本脚本。"
fi

ensure_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    info "未找到 Homebrew，开始安装。可能会要求输入 macOS 密码。"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi

  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1 || fail "Homebrew 安装后仍无法找到 brew。"
}

brew_formula() {
  local package="$1"
  if brew list --formula "$package" >/dev/null 2>&1; then
    info "$package 已安装，跳过。"
  else
    info "安装 Homebrew 软件包：$package"
    brew install "$package"
  fi
}

brew_cask() {
  local package="$1"
  if brew list --cask "$package" >/dev/null 2>&1; then
    info "$package 已安装，跳过。"
  else
    info "安装 macOS 应用/工具：$package"
    brew install --cask "$package"
  fi
}

ensure_brew
brew update
brew_formula uv
brew_formula node
brew_cask android-studio
brew_cask android-commandlinetools

if [[ "${WEN_INSTALL_SCRCPY:-1}" == "1" ]]; then
  brew_formula scrcpy
fi

if [[ ! -x "$WEN_JAVA_PATH/bin/java" ]]; then
  if command -v java >/dev/null 2>&1; then
    WEN_JAVA_PATH="$(/usr/libexec/java_home 2>/dev/null || true)"
  fi
fi
[[ -x "${WEN_JAVA_PATH:-}/bin/java" ]] || fail "未找到 Java。请先打开 Android Studio 完成初始化，或设置 JAVA_HOME。"

WEN_SDKMANAGER=""
for candidate in \
  "$WEN_SDK_PATH/cmdline-tools/latest/bin/sdkmanager" \
  "/opt/homebrew/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager" \
  "/usr/local/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager"; do
  if [[ -x "$candidate" ]]; then
    WEN_SDKMANAGER="$candidate"
    break
  fi
done
[[ -n "$WEN_SDKMANAGER" ]] || fail "未找到 Android sdkmanager。请打开 Android Studio 完成 SDK 初始化后重试。"

export ANDROID_HOME="$WEN_SDK_PATH"
export ANDROID_SDK_ROOT="$WEN_SDK_PATH"
export JAVA_HOME="$WEN_JAVA_PATH"
WEN_SDKMANAGER_DIR="$(dirname "$WEN_SDKMANAGER")"
export PATH="$WEN_JAVA_PATH/bin:$WEN_SDKMANAGER_DIR:$WEN_SDK_PATH/platform-tools:$PATH"

mkdir -p "$WEN_SDK_PATH"
info "安装 Android Platform-Tools（ADB）。"
yes | "$WEN_SDKMANAGER" --sdk_root="$WEN_SDK_PATH" --licenses >/dev/null || true
"$WEN_SDKMANAGER" --sdk_root="$WEN_SDK_PATH" "platform-tools"
[[ -x "$WEN_SDK_PATH/platform-tools/adb" ]] || fail "ADB 安装失败：$WEN_SDK_PATH/platform-tools/adb"

info "安装 Python 依赖（Appium 客户端、RapidOCR、测试工具）。"
cd "$WEN_PROJECT_ROOT"
uv sync --extra appium --extra ocr --extra dev

if command -v appium >/dev/null 2>&1; then
  info "Appium 已安装，跳过 npm 安装。"
else
  info "安装 Appium 服务。"
  npm install --global appium
fi

command -v appium >/dev/null 2>&1 || fail "Appium 安装后仍无法找到 appium。"
if appium driver list --installed 2>/dev/null | grep -qi 'uiautomator2'; then
  info "Appium UiAutomator2 驱动已安装，跳过。"
else
  info "安装 Appium UiAutomator2 驱动。"
  appium driver install uiautomator2
fi

mkdir -p "$WEN_PROJECT_ROOT/data/evidence" "$WEN_PROJECT_ROOT/outputs" "$WEN_PROJECT_ROOT/work"

info "环境安装完成。"
printf '\n下一步：\n'
printf '  1. 用 USB 连接 Android 手机，开启开发者模式和 USB 调试。\n'
printf '  2. 执行：adb devices（手机上点击“允许调试”）。\n'
printf '  3. 终端一启动 Appium：./scripts/start-appium.sh\n'
printf '  4. 终端二启动 Wen：uv run wen serve --host 127.0.0.1 --port 8765\n'
printf '  5. 浏览器打开：http://127.0.0.1:8765\n'
printf '\n如不需要 scrcpy，可用 WEN_INSTALL_SCRCPY=0 ./scripts/setup-mac.sh 跳过。\n'
