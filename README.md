<!-- Language Switcher -->
<p align="right">
  <a href="#english">English</a> |
  <a href="#chinese">中文</a>
</p>

---

<a id="english"></a>

# AutoRUN v1

A universal AI coding assistant supporting OpenAI and Anthropic compatible APIs.

## Installation

Requires Python 3.8+.

### Quick Install (recommended)

**Windows:**
```cmd
install.bat
```

**macOS / Linux:**
```bash
./install.sh
```

This creates a virtual environment, installs dependencies, and sets up the `autorun` command.

### Manual Install

```bash
cd AutoRUN_v1
pip install -e .
```

After installation, the `autorun` command is available system-wide:

```bash
autorun --version
```

### Alternative: pip + requirements.txt

```bash
cd AutoRUN_v1
pip install -r requirements.txt
python cli.py
```

## Quick Start

```bash
autorun
```

On first run, AutoRUN will guide you through API configuration interactively:

```
╔══════════════════════════════════════════╗
║     AutoRUN v1.0 — 首次设置             ║
╚══════════════════════════════════════════╝

选择 API 类型 [1=OpenAI, 2=Anthropic] [默认 openai]: 1
API URL [默认 https://api.openai.com]:
API Key: sk-xxxxxxxxxxxx
模型名称 [默认 gpt-4o]:

✓ 配置已保存！
```

Config is stored at `~/.autorun/config.json`.

## API Configuration

Three methods, listed by priority:

### Method 1: First-run Setup Wizard

```bash
autorun
```
Follow the interactive prompts on first run, or run `autorun --setup` to reconfigure anytime.

### Method 2: Environment Variables

**Windows (PowerShell):**
```powershell
$env:AUTORUN_API_TYPE = "openai"
$env:AUTORUN_API_URL  = "https://api.openai.com"
$env:AUTORUN_API_KEY  = "sk-xxxxxxxxxxxx"
$env:AUTORUN_MODEL    = "gpt-4o"
autorun
```

**Windows (CMD):**
```cmd
set AUTORUN_API_TYPE=openai
set AUTORUN_API_URL=https://api.openai.com
set AUTORUN_API_KEY=sk-xxxxxxxxxxxx
set AUTORUN_MODEL=gpt-4o
autorun
```

**macOS / Linux:**
```bash
export AUTORUN_API_TYPE=openai
export AUTORUN_API_URL=https://api.openai.com
export AUTORUN_API_KEY=sk-xxxxxxxxxxxx
export AUTORUN_MODEL=gpt-4o
autorun
```

### Method 3: Configure inside REPL

```bash
autorun
```

Inside the REPL:
```
/api type openai                          # API type: openai or anthropic
/api url https://api.openai.com           # API base URL
/api key sk-xxxxxxxxxxxx                  # Your API key
/model gpt-4o                             # Model name
```

## Usage

### CLI REPL (default interactive mode)

```bash
autorun
```

### Web UI

```bash
autorun --web
```

Default: http://127.0.0.1:8765

```bash
# Custom port and host
autorun --web --port 8080 --host 0.0.0.0
```

### Pipe Mode

```bash
echo "Explain this code" | autorun --print
autorun --print "Explain this code"
```

### Reconfigure API

```bash
autorun --setup
```

## CLI Options

| Flag | Description |
|------|-------------|
| `--version`, `-V`, `-v` | Show version |
| `--setup` | Re-run API setup wizard |
| `--web` | Start Web UI server |
| `--print`, `-p` | Pipe/print mode (non-interactive) |
| `--port <N>` | Web UI port (default: 8765) |
| `--host <HOST>` | Web UI host (default: 127.0.0.1) |
| `-m <model>`, `--model <model>` | Override default model |
| `-d <path>`, `--dir <path>` | Working directory |
| `--context <N>` | Context window size (tokens) |

## REPL Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/api` | Show current API config |
| `/api type <openai\|anthropic>` | Set API type |
| `/api url <URL>` | Set API base URL |
| `/api key <KEY>` | Set API key |
| `/model <name>` | Set model name |
| `/context [tokens]` | Show/set context window size |
| `/status` | Show session status |
| `/clear` | Clear conversation history |
| `/compact` | Compact conversation context |
| `/memory` | Show memory status |
| `/skill` | List available skills |
| `/todos` | Show todo list |
| `/fast` | Toggle fast mode |
| `/exit` | Exit |

## Supported APIs

- **OpenAI Compatible** — OpenAI official API & all `/v1/chat/completions` compatible services (OpenAI, Azure OpenAI, DeepSeek, Groq, etc.)
- **Anthropic Compatible** — Anthropic official API & all `/v1/messages` compatible services (Claude series)

## Configuration Storage

```
~/.autorun/
├── config.json       # API key, URL, type, model, context window
├── history           # Input history
├── memory/           # Memory system
└── skills/           # User skills
```

- Environment variables take priority over config.json
- API key is stored as plain text (same as `.env` files)

## OCR (Optical Character Recognition)

AutoRUN includes a built-in OCR tool based on [Falcon-OCR](https://huggingface.co/tiiuae/Falcon-OCR), a lightweight 0.3B vision-language model that runs entirely on your local machine.

### Features

- **Plain OCR** — Full-page text extraction for documents, screenshots, receipts, slides
- **Layout-aware OCR** — Detects text regions (tables, formulas, headers, captions) and extracts per-region text. Best for complex multi-column documents and academic papers.
- **Fully local** — All inference runs on your GPU/CPU. No image data leaves your machine.
- **Token saving** — Non-multimodal models can "see" images by calling OCR first, then analyzing the extracted text.
- **Auto-download** — Model (~600MB) is downloaded on first use. Supports Chinese mirrors (`HF_ENDPOINT=https://hf-mirror.com`).

### Usage

The OCR tool is automatically available in the REPL and Web UI. The AI will call it when you ask it to read text from an image:

```
> 帮我把这张截图里的文字提取出来
```

Or use with explicit mode:

```
> 用 layout 模式 OCR 这篇论文的截图
```

### Configuration

| Environment Variable | Description | Default |
|----------------------|-------------|---------|
| `HF_ENDPOINT` | HuggingFace mirror (set to `https://hf-mirror.com` for China) | `https://huggingface.co` |
| `AUTORUN_OCR_LOCAL_DIR` | Local model directory (skip download) | Auto-download |
| `AUTORUN_OCR_DEVICE` | Inference device (`cuda`/`cpu`) | Auto-detect |
| `AUTORUN_OCR_DTYPE` | Model dtype (`float32`/`float16`/`bfloat16`) | `float32` |

### Requirements

- CUDA GPU recommended (CPU works but slower)
- Dependencies auto-installed on first use: `torch`, `transformers`, `torchvision`, `huggingface_hub`, `safetensors`, `tokenizers`, `Pillow`

## Project Structure

```
AutoRUN_v1/
├── cli.py              # Entry point (autorun command)
├── main.py             # CLI argument parsing & routing
├── commands.py         # Slash command system
├── query.py            # Core query loop
├── query_engine.py     # High-level conversation orchestrator
├── pyproject.toml      # Package metadata & dependencies
├── requirements.txt    # Pip dependencies (legacy)
├── install.bat         # Windows one-click installer
├── install.sh          # macOS/Linux one-click installer
├── api/                # API clients (OpenAI / Anthropic)
├── context/            # Context building (git, environment, etc.)
├── messages/           # Message types & utilities
├── prompts/            # System prompts
├── services/           # Compaction, LSP, etc.
├── skills/             # Skill discovery & loading
├── state/              # Session state management
├── tools/              # Tool registry & execution
│   └── ocr_engine/     # Falcon-OCR inference engine (Apache-2.0, from TII)
├── ui/                 # CLI / Web UI
│   ├── cli/            # prompt_toolkit / Textual REPL
│   └── web/            # FastAPI Web server + frontend
└── utils/              # Config, tokens, env utils
```

---

<a id="chinese"></a>

# AutoRUN v1

通用 AI 编程助手，支持 OpenAI 和 Anthropic 兼容 API。

## 安装

需要 Python 3.8+。

### 一键安装（推荐）

**Windows:**
```cmd
install.bat
```

**macOS / Linux:**
```bash
./install.sh
```

自动创建虚拟环境、安装依赖，并配置 `autorun` 命令。

### 手动安装

```bash
cd AutoRUN_v1
pip install -e .
```

安装后，`autorun` 命令在终端中全局可用：

```bash
autorun --version
```

### 备选：pip + requirements.txt

```bash
cd AutoRUN_v1
pip install -r requirements.txt
python cli.py
```

## 快速开始

```bash
autorun
```

首次运行会自动引导你配置 API：

```
╔══════════════════════════════════════════╗
║     AutoRUN v1.0 — 首次设置             ║
╚══════════════════════════════════════════╝

选择 API 类型 [1=OpenAI, 2=Anthropic] [默认 openai]: 1
API URL [默认 https://api.openai.com]:
API Key: sk-xxxxxxxxxxxx
模型名称 [默认 gpt-4o]:

✓ 配置已保存！
```

配置保存在 `~/.autorun/config.json`。

## API 配置

三种配置方式，按优先级排序：

### 方式一：首次运行向导

```bash
autorun
```

首次运行自动触发，或通过 `autorun --setup` 随时重新配置。

### 方式二：环境变量

**Windows (PowerShell):**
```powershell
$env:AUTORUN_API_TYPE = "openai"
$env:AUTORUN_API_URL  = "https://api.openai.com"
$env:AUTORUN_API_KEY  = "sk-xxxxxxxxxxxx"
$env:AUTORUN_MODEL    = "gpt-4o"
autorun
```

**Windows (CMD):**
```cmd
set AUTORUN_API_TYPE=openai
set AUTORUN_API_URL=https://api.openai.com
set AUTORUN_API_KEY=sk-xxxxxxxxxxxx
set AUTORUN_MODEL=gpt-4o
autorun
```

**macOS / Linux:**
```bash
export AUTORUN_API_TYPE=openai
export AUTORUN_API_URL=https://api.openai.com
export AUTORUN_API_KEY=sk-xxxxxxxxxxxx
export AUTORUN_MODEL=gpt-4o
autorun
```

### 方式三：启动后在 REPL 中设置

```bash
autorun
```

进入 REPL 后输入：
```
/api type openai                          # API 类型: openai 或 anthropic
/api url https://api.openai.com           # API 基础 URL
/api key sk-xxxxxxxxxxxx                  # 你的 API 密钥
/model gpt-4o                             # 模型名称
```

配置保存在 `~/.autorun/config.json`，下次启动自动加载。

## 运行方式

### CLI REPL（默认交互模式）

```bash
autorun
```

### Web UI

```bash
autorun --web
```

默认访问 http://127.0.0.1:8765

```bash
# 自定义端口和地址
autorun --web --port 8080 --host 0.0.0.0
```

### 管道模式

```bash
echo "解释一下这段代码" | autorun --print
autorun --print "解释一下这段代码"
```

### 重新配置 API

```bash
autorun --setup
```

## 命令行选项

| 选项 | 说明 |
|------|------|
| `--version`, `-V`, `-v` | 显示版本号 |
| `--setup` | 重新运行 API 设置向导 |
| `--web` | 启动 Web UI 服务器 |
| `--print`, `-p` | 管道/打印模式（非交互） |
| `--port <N>` | Web UI 端口（默认: 8765） |
| `--host <HOST>` | Web UI 地址（默认: 127.0.0.1） |
| `-m <model>`, `--model <model>` | 覆盖默认模型 |
| `-d <path>`, `--dir <path>` | 工作目录 |
| `--context <N>` | 上下文窗口大小（tokens） |

## REPL 常用命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/api` | 查看当前 API 配置 |
| `/api type <openai\|anthropic>` | 设置 API 类型 |
| `/api url <URL>` | 设置 API 基础 URL |
| `/api key <KEY>` | 设置 API 密钥 |
| `/model <name>` | 设置模型名称 |
| `/context [tokens]` | 显示/设置上下文窗口大小 |
| `/status` | 显示会话状态 |
| `/clear` | 清除对话历史 |
| `/compact` | 压缩对话上下文 |
| `/memory` | 显示记忆系统状态 |
| `/skill` | 列出可用 skill |
| `/todos` | 显示 todo 列表 |
| `/fast` | 切换快速模式 |
| `/exit` | 退出 |

## 支持的 API

- **OpenAI 兼容** — OpenAI 官方 API 及所有兼容 `/v1/chat/completions` 的服务（OpenAI, Azure OpenAI, DeepSeek, 硅基流动, Groq 等）
- **Anthropic 兼容** — Anthropic 官方 API 及所有兼容 `/v1/messages` 的服务（Claude 系列）

## 配置存储

```
~/.autorun/
├── config.json       # API key, URL, type, model, context window
├── history           # 输入历史
├── memory/           # 记忆系统
└── skills/           # 用户 skill
```

- 环境变量优先级高于 config.json
- API key 明文存储（与 `.env` 文件方式相同）

## OCR (光学字符识别)

AutoRUN 内置基于 [Falcon-OCR](https://huggingface.co/tiiuae/Falcon-OCR) 的 OCR 工具，0.3B 参数的轻量视觉语言模型，完全在本地运行。

### 功能

- **全页 OCR (plain)** — 全文提取，适合文档、截图、收据、幻灯片
- **布局感知 OCR (layout)** — 自动检测文字区域（表格、公式、标题、页眉等）并分别提取。适合复杂多栏文档和学术论文
- **完全本地** — 所有推理在 GPU/CPU 上完成，图片数据不会离开本机
- **节省 Token** — 非多模态模型可先调用 OCR 提取文字，再对文字分析推理
- **自动下载** — 首次使用时自动下载模型（约 600MB），支持国内镜像加速（`HF_ENDPOINT=https://hf-mirror.com`）

### 使用方式

OCR 工具在 REPL 和 Web UI 中自动可用。当你要求 AI 从图片中提取文字时，AI 会自动调用：

```
> 帮我把这张截图里的文字提取出来
```

或显式指定模式：

```
> 用 layout 模式 OCR 这篇论文的截图
```

### 配置

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `HF_ENDPOINT` | HuggingFace 镜像（国内设为 `https://hf-mirror.com`） | `https://huggingface.co` |
| `AUTORUN_OCR_LOCAL_DIR` | 本地模型目录（跳过下载） | 自动下载 |
| `AUTORUN_OCR_DEVICE` | 推理设备 (`cuda`/`cpu`) | 自动检测 |
| `AUTORUN_OCR_DTYPE` | 模型数据类型 (`float32`/`float16`/`bfloat16`) | `float32` |

### 依赖

- 推荐 CUDA GPU（CPU 可用但较慢）
- 依赖在首次使用时自动安装：`torch`, `transformers`, `torchvision`, `huggingface_hub`, `safetensors`, `tokenizers`, `Pillow`

## 项目结构

```
AutoRUN_v1/
├── cli.py              # 入口点（autorun 命令）
├── main.py             # CLI 参数解析和路由
├── commands.py         # 斜杠命令系统
├── query.py            # 核心查询循环
├── query_engine.py     # 高层对话编排
├── pyproject.toml      # 包元数据和依赖声明
├── requirements.txt    # Pip 依赖（兼容旧方式）
├── install.bat         # Windows 一键安装脚本
├── install.sh          # macOS/Linux 一键安装脚本
├── api/                # API 客户端 (OpenAI / Anthropic)
├── context/            # 上下文构建 (git, 环境等)
├── messages/           # 消息类型和工具
├── prompts/            # 系统提示词
├── services/           # 压缩、LSP 等服务
├── skills/             # 技能加载
├── state/              # 会话状态
├── tools/              # 工具注册和执行
│   └── ocr_engine/     # Falcon-OCR 推理引擎 (Apache-2.0, from TII)
├── ui/                 # CLI / Web UI
│   ├── cli/            # prompt_toolkit / Textual REPL
│   └── web/            # FastAPI Web 服务器 + 前端
└── utils/              # 配置、token 等工具
```

---

## 致谢 / Acknowledgments

本项目的 OCR 功能基于 [Falcon-Perception](https://github.com/tiiuae/Falcon-Perception) 项目，使用 [Falcon-OCR](https://huggingface.co/tiiuae/Falcon-OCR) 模型。

Falcon-Perception 和 Falcon-OCR 由 [Technology Innovation Institute (TII)](https://tii.ae/), UAE 开发，以 Apache-2.0 许可证开源发布。

> **引用 / Citation:**
> Bevli et al., *Falcon-Perception*, arXiv:2603.27365, 2026.

The OCR engine source code from Falcon-Perception is integrated under `tools/ocr_engine/`, licensed under Apache-2.0.

---

<!-- Language Switcher -->
<p align="center">
  <a href="#english"><b>English</b></a> |
  <a href="#chinese"><b>中文</b></a>
</p>
