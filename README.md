# Wen 数据监查 Demo

Wen 是一个运行在 macOS 上、通过 Android 真机采集抖音公开展示商品数据的本地工具。

```text
Python → Appium Client → Appium Server → UiAutomator2 → Android 真机
                                              ↘ ADB 截图、滑动与 UI XML 兜底
```

项目不依赖 Codex、GPT 或在线大模型。OCR 使用本地 RapidOCR；查询条件、结果、截图和 UI XML 默认只保存在本机。

## 功能

- 查询我收藏的商品：读取当前账号收藏的全部有效商品并排除失效条目。
- 查询店铺内的商品：通过完整店铺名称搜索，或使用 `sec_shop_id` 直达店铺；支持综合、销量、上新、价格升序/降序和数量限制。
- 精准查询：输入一个或多个商品 ID，逐个直达商品详情页；也可以从抖音商品分享文本中提取 ID。
- 采集商品名称、主图、价格、展示销量等页面可见信息，保持抖音列表原始顺序。
- 保存多条查询条件，一次运行多个条件；支持在线查看、XLSX 下载、历史分页和单条记录删除。
- 使用 SQLite 保存结构化结果，并保留必要的截图和 UI XML 便于排查。

当前只支持 macOS + Android 真机。手机需要提前安装并登录抖音。

## Install

### 1. Clone

```bash
git clone https://github.com/zhaozejun0909/wencheng.git
cd wencheng
```

### 2. 安装环境

```bash
./scripts/setup-mac.sh
```

脚本会安装或配置 Homebrew、uv、Node.js、Android Studio、ADB、Appium、UiAutomator2、RapidOCR、项目依赖和 scrcpy。已经安装的组件会自动跳过。

首次执行时，如果系统要求安装 Command Line Tools，或 Android Studio 尚未完成初始化，按提示完成后重新运行脚本即可。

不需要 scrcpy 时可以执行：

```bash
WEN_INSTALL_SCRCPY=0 ./scripts/setup-mac.sh
```

### 3. 连接手机

1. 开启 Android 开发者选项和 USB 调试。
2. 用 USB 连接 Mac，并在手机上允许 USB 调试。
3. 确认设备状态：

```bash
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
adb devices -l
```

设备状态应为 `device`。如果连接了多台设备，可以创建本地配置：

```bash
cp .env.example .env
```

在 `.env` 中填写：

```dotenv
WEN_DEVICE_SERIAL=你的设备序列号
WEN_OCR_PROVIDER=rapidocr
```

## 启动

在两个终端中分别执行：

```bash
# 终端一
./scripts/start-appium.sh
```

```bash
# 终端二
uv run wen serve --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>，新增查询条件并开始查询。

Web 页面提供三种查询方式：

1. 精准查询：填写商品 ID，或粘贴商品分享文本提取 ID。
2. 查询我收藏的商品：读取当前账号收藏的有效商品。
3. 查询店铺内的商品：按完整店铺名称搜索，或填写 `sec_shop_id` 直达店铺。

手机登录、验证码和安全验证需要人工完成。需要在 Mac 上查看手机画面时可以运行：

```bash
scrcpy
```

## 测试

离线测试使用 `fixtures/` 中的页面夹具，不需要连接手机：

```bash
uv sync --extra appium --extra ocr --extra dev
uv run pytest -q
uv run ruff check .
```

## 停止

在 Wen 和 Appium 所在终端分别按 `Ctrl-C`。

Mac 进入睡眠后，USB、Appium 和 Web 服务可能暂停或断开；唤醒后应检查并按需重新启动。

## 数据位置

- `data/wen.sqlite3`：查询条件、任务和结果。
- `data/evidence/`：截图、UI XML 和解析证据。
- `data/exports/`：生成的 XLSX 文件。

`data/`、`.env` 和 `.venv/` 都是本地内容，不会提交到 Git。

## 目录结构

```text
src/wen/device/       Appium + UiAutomator2 + ADB 设备控制
src/wen/platforms/    抖音页面与字段解析
src/wen/workflows/    查询和采集状态机
src/wen/extract/      UI XML 与本地 OCR
src/wen/web/          FastAPI 接口与 Web 页面
fixtures/             离线页面夹具
tests/                单元、接口和工作流测试
scripts/              macOS 环境安装与服务启动脚本
```

## 常见问题

### 无法连接 Appium，提示 127.0.0.1:4723 Connection refused

Appium 没有启动，先运行：

```bash
./scripts/start-appium.sh
```

### ADB 显示 unauthorized

解锁手机，重新插拔 USB，并在手机上重新确认调试授权。

### 服务已启动，但无法控制手机

确认 `adb devices -l` 显示 `device`、Appium 仍在运行，并且抖音已经登录。

早期架构记录见 [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md)，当前功能和使用方式以本 README 为准。
