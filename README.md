# Wen 数据监查 Demo

Wen 是一个运行在 macOS 上、通过真实 Android 手机采集抖音公开展示商品数据的本地 Demo。采集链路固定为：

```text
Python → Appium Client → Appium Server → UiAutomator2 → Android 真机
                                              ↘ ADB 截图、滑动与 UI XML 兜底
```

项目可以独立运行，不依赖 Codex、GPT 或在线大模型。OCR 使用本地 RapidOCR；数据、截图和查询条件默认只保存在本机。

## 当前功能

- 查询我收藏的商品：进入商品收藏完整列表，读取全部有效商品并排除失效条目。
- 查询店铺内的商品：通过完整店铺名称搜索，或使用 `sec_shop_id` 直达店铺；支持综合、销量、上新、价格升序/降序和数量限制。
- 精准查询：输入一个或多个商品 ID，逐个直达商品详情页采集；支持从抖音商品分享文本中提取商品 ID。
- 采集商品名称、主图、价格、展示销量等页面可见信息，结果保持抖音列表原始顺序，不按标题去重或在本地重新排序。
- 保存多条查询条件，一次运行多个条件；结果按一次查询归档，并支持在线查看、XLSX 下载和单条历史记录删除。
- SQLite 保存结构化结果，同时保留必要的 UI XML、截图和解析证据，便于排查采集差异。

当前只支持 macOS + Android 真机。抖音需要提前安装并由用户手动登录。

## 上传 GitHub 时应包含什么

建议提交：

- `src/`：应用代码。
- `tests/`、`fixtures/`：离线回归测试和页面夹具。
- `scripts/`：macOS 安装脚本和 Appium 启动脚本。
- `pyproject.toml`、`uv.lock`：项目与锁定依赖。
- `.env.example`、`.gitignore`、`README.md`。
- `TECHNICAL_PLAN.md`：早期设计记录；当前行为以本 README 为准。

不要提交：

- `.venv/`：本机 Python 虚拟环境，体积很大且不可跨机器复用。
- `data/`：SQLite 数据库、查询记录、真实商品截图和 UI XML。
- `outputs/`、`work/`：运行时导出和临时文件。
- `.env`：可能包含设备配置或 API Key。
- `__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.DS_Store` 等缓存。

这些内容已经由 `.gitignore` 排除。

## 新 Mac 一键安装

### 1. 准备代码

第一次使用命令行 Git 时，macOS 需要 Command Line Tools。它提供 Git、编译器和部分 Python 依赖所需的基础工具：

```bash
xcode-select --install
```

安装完成后，克隆仓库并进入项目目录：

```bash
git clone <你的 GitHub 仓库地址>
cd <仓库目录>
```

如果是通过 ZIP 下载代码，也可以直接进入解压后的项目目录。

### 2. 运行一键安装脚本

```bash
./scripts/setup-mac.sh
```

脚本会幂等安装或配置：

- Homebrew、uv、Node.js。
- Android Studio、Android Command Line Tools 和 ADB。
- Appium 与 UiAutomator2 Driver。
- 项目的 Python 依赖、RapidOCR 和测试依赖。
- scrcpy，方便在 Mac 上查看和操作手机。

第一次执行时可能出现两种需要人工完成的步骤：

- macOS 弹出 Command Line Tools 安装窗口：安装完成后重新运行脚本。
- Android Studio 尚未初始化 SDK/JBR：打开一次 Android Studio 完成初始化，再重新运行脚本。

可选参数：

```bash
# 不安装 scrcpy
WEN_INSTALL_SCRCPY=0 ./scripts/setup-mac.sh

# 同时安装 Cloudflare 临时外网测试工具
WEN_INSTALL_CLOUDFLARED=1 ./scripts/setup-mac.sh
```

### 3. 连接并准备手机

1. 在 Android 手机安装并登录抖音。
2. 开启开发者选项和 USB 调试。
3. 用 USB 连接 Mac，在手机上同意“允许 USB 调试”。
4. 在项目目录确认设备：

```bash
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
adb devices -l
```

状态应为 `device`，不能是 `unauthorized`。如果连接了多台设备，把目标序列号写入本地配置：

```bash
cp .env.example .env
```

然后编辑 `.env`：

```dotenv
WEN_DEVICE_SERIAL=你的设备序列号
WEN_OCR_PROVIDER=rapidocr
```

`.env` 只供本机使用，不会提交到 Git。

## 启动和使用

需要两个终端窗口，两个命令都在项目目录执行。

终端一启动 Appium：

```bash
./scripts/start-appium.sh
```

终端二启动 Wen Web 服务：

```bash
uv run wen serve --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>，新增查询条件并开始查询。

Web 页面提供三种方式：

1. 精准查询：填写商品 ID，或粘贴商品分享文本自动提取 ID。
2. 查询我收藏的商品：读取当前抖音账号收藏的全部有效商品。
3. 查询店铺内的商品：按完整店铺名称搜索，或填写 `sec_shop_id` 直达店铺，再选择排序和数量。

手机登录、验证码和安全验证需要人工完成。任务遇到这些页面会停止或给出错误，不会自动处理验证。

需要在 Mac 上查看手机画面时，另开终端运行：

```bash
scrcpy
```

## 临时外网测试

确认本地页面可正常打开后，可以启动临时 Cloudflare Tunnel：

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

命令会输出一个随机的 `https://*.trycloudflare.com` 地址。该地址每次重启都会变化，并且会把本地控制台暴露给持有链接的人，只适合短时间测试。测试结束后在对应终端按 `Ctrl-C` 停止。

Mac 进入睡眠后，USB、Appium、Web 服务和临时隧道都可能暂停或断开；唤醒后应检查并按需重新启动。

## 停止服务

分别在 Wen、Appium、Cloudflare Tunnel 所在终端按 `Ctrl-C`。确认端口已释放：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:4723 -sTCP:LISTEN
```

没有输出即表示对应服务已经停止。

## 离线测试

离线测试不连接手机，使用 `fixtures/` 中的页面夹具验证解析、存储、接口和交互逻辑：

```bash
uv sync --extra appium --extra ocr --extra dev
uv run pytest -q
uv run ruff check .
```

离线测试通过不等于真机链路通过。抖音页面会变化，发布前仍应完成真机验收。

## 从 GitHub 全新拉取后的验收流程

建议在另一个目录或另一台 Mac 执行：

1. `git clone` 全新仓库，不复制原项目的 `.venv`、`data` 或 `.env`。
2. 运行 `./scripts/setup-mac.sh`，确认重复执行不会报错。
3. 运行 `uv run pytest -q` 和 `uv run ruff check .`。
4. 连接已登录抖音的 Android 真机，确认 `adb devices -l` 状态为 `device`。
5. 启动 Appium 和 Wen，确认首页能打开并能保存查询条件。
6. 分别验证精准查询、收藏查询、店铺商品查询各一次。
7. 对照手机页面检查商品数量、顺序、名称、价格、销量和图片。
8. 验证在线结果、XLSX 下载、历史分页与单条记录删除。
9. 如需外部测试，再启动临时 Cloudflare Tunnel。

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
data/                 本地数据库和证据，运行时生成且不提交
```

## 常见问题

### 无法连接 Appium，提示 127.0.0.1:4723 Connection refused

Appium 没有启动。先运行：

```bash
./scripts/start-appium.sh
```

### ADB 显示 unauthorized

解锁手机，重新插拔 USB，并在手机上同意调试授权。必要时撤销 USB 调试授权后重新连接。

### 服务已启动，但查询无法控制手机

依次确认：手机仍通过 USB 连接、`adb devices -l` 为 `device`、Appium 终端仍在运行、抖音已经登录。

### 数据保存在哪里

默认位于 `data/wen.sqlite3` 和 `data/evidence/`。这些都是本地运行数据，已被 Git 忽略。

## 说明

- `uv.lock` 应与代码一起提交，用于稳定另一台 Mac 的依赖版本。
- `TECHNICAL_PLAN.md` 是早期技术方案记录，可能包含已被当前实现替代的交互描述。
- 项目尚未选择开源许可证；上传前可以先保持无许可证，或由仓库所有者明确选择 MIT、Apache-2.0 等许可证。
