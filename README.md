# Agent Token Saver / AI Coding Agent Token Saver

<p align="center">
  <strong>一键减少 AI Coding Agent 的 Token 消耗 / Reduce Token Consumption for AI Coding Agents</strong>
  <br/>
  支持 Claude Code · Codex · OpenClaw · Cursor · Aider · Continue · Windsurf
  <br/>
  <em>Supporting Claude Code, Codex, OpenClaw, Cursor, Aider, Continue, Windsurf</em>
</p>

<p align="center">
  <a href="#安装-installation">安装 / Installation</a> ·
  <a href="#快速开始-quick-start">快速开始 / Quick Start</a> ·
  <a href="#功能详解-features">功能 / Features</a> ·
  <a href="#配置-configuration">配置 / Configuration</a> ·
  <a href="#可移植性-portability">可移植性 / Portability</a> ·
  <a href="#架构-architecture">架构 / Architecture</a>
</p>

---

## 为什么需要 Agent Token Saver？ / Why Agent Token Saver?

AI Coding Agent 在每次工具调用时都会把文件内容塞进上下文。一个大型项目动辄数十万 token，单次 `Read` 就可能吃掉几千 token。

Every time an AI Coding Agent invokes a tool, file contents are injected into the context. A large project can easily reach hundreds of thousands of tokens, and a single `Read` call may consume thousands of tokens.

**Agent Token Saver 从四个层面压缩 token 消耗 / Reduces token consumption through four strategies：**

| 层面 / Layer | 手段 / Method | 典型节省 / Typical Savings |
|------|------|------|
| 输入压缩 / Input Compression | 去注释、去 docstring、骨架提取、智能截断 / Strip comments, docstrings, skeleton extraction, smart truncation | 40-80% |
| 去重 / Deduplication | 内容 hash 去重、结构去重、SimHash 近似去重 / Hash-based, structural, SimHash fuzzy dedup | 10-30% |
| 输出最小化 / Output Minimization | Hook 输出截断、压缩、字段省略 / Truncation, compression, field omission | 50-90% |
| 增量上下文 / Incremental Context | 只发送变更文件、路径缩写、目录索引 / Changed files only, path abbreviation, directory index | 60-95% |

---

## 安装 / Installation

### 一键安装（推荐） / One-Click Install (Recommended)

```bash
pip install agent-token-saver && ats-setup
```

`ats-setup` 是全自动安装命令，零交互完成：**检测 Agent → 安装 Hooks → 启动 Daemon**。

`ats-setup` is a fully automated installer: **Detect Agent → Install Hooks → Start Daemon**.

### NPX 运行（无需安装） / Run via NPX (No Install Required)

```bash
npx agent-token-saver setup
```

通过 `npx` 直接运行，无需全局安装 Python 包。适合 CI/CD 或一次性使用。

Run directly via `npx` without installing the Python package globally. Ideal for CI/CD or one-time use.

### 手动安装 / Manual Install

```bash
# 克隆项目 / Clone the repository
git clone https://github.com/MQ-HZY-For-Cute/agent-token-saver.git
cd agent-token-saver

# 安装依赖 / Install dependencies
pip install -e .

# 自动检测并安装 / Auto-detect and install
ats setup
```

### 系统要求 / System Requirements

- Python >= 3.10
- Node.js >= 16.0（仅 npx 模式需要 / required for npx mode only）
- Windows 10+ / macOS 10.15+ / Linux (any modern distro)
- 无额外系统依赖（纯 Python 标准库 + 少量常用包 / Pure Python stdlib + common packages）

---

## 快速开始 / Quick Start

### 新用户（零配置） / New Users (Zero Config)

```bash
# 一步完成全部初始化 / One-step setup
ats-setup
```

这会自动检测你当前使用的 AI Coding Agent，安装对应的 Hook，并启动后台监控 Daemon。

This auto-detects your AI Coding Agent, installs the corresponding Hook, and starts the background Daemon.

### 验证安装 / Verify Installation

```bash
# 检查状态 / Check status
ats doctor

# 测试 Hook / Test Hook
ats hooks test -t Read
```

### 常用工作流 / Common Workflows

```bash
# 压缩文件上下文后发给 Agent / Compress file context for Agent
ats prep files src/ -o context.md

# 管道模式（直接喂给 Agent） / Pipe mode (feed directly to Agent)
find src/ -name "*.py" | ats prep pipe | claude "分析这段代码"

# 查看 Token 消耗报告 / View token consumption report
ats stats report

# 启动后台监控 / Start background monitoring
ats daemon start
```

---

## 功能详解 / Features

### 1. `prep` — 智能预处理 / Smart Preprocessing

将文件内容压缩为 Agent 友好的上下文。

Compress file content into Agent-friendly context.

```bash
ats prep files <paths...>     # 处理文件，去注释/去重/截断 / Process files
ats prep prompt <text>        # 压缩 prompt 文本 / Compress prompt text
ats prep diff <a> <b>         # 对比两个路径的处理效果 / Compare processing results
ats prep pipe                 # 管道模式（stdin → stdout） / Pipe mode (stdin → stdout)
```

**核心能力 / Core capabilities：**

- 自动去除 16+ 种语言的注释（保留 TODO/FIXME/HACK/NOTE）/ Auto-strip comments in 16+ languages (preserves TODO/FIXME/HACK/NOTE)
- Python docstring 可选去除 / Optional Python docstring removal
- 内容去重（按内容 hash），避免重复读取同一文件 / Content dedup (hash-based)
- 大文件智能截断（保留头尾，中间折叠）/ Smart truncation for large files
- 空类体压缩、冗余 `pass` 去除、`__future__` import 清洗 / Empty class compression, redundant `pass` removal
- SimHash 近似重复检测 / SimHash fuzzy duplicate detection
- 目录索引生成（渐进式披露，不读取文件内容）/ Directory index generation (progressive disclosure)
- 自适应 detail_level（根据 token 预算自动选择 full/stripped/skeleton/block）/ Adaptive detail_level

### 2. `agents` — 多 Agent 适配器管理 / Multi-Agent Adapter Management

统一的 Agent 管理接口，支持 7+ AI Coding Agent。

Unified Agent management interface, supporting 7+ AI Coding Agents.

```bash
ats agents list               # 列出所有 Agent 及安装状态 / List all agents
ats agents detect             # 自动检测当前运行的 Agent / Auto-detect current agent
ats agents setup              # 一键检测 + 安装 + 启动 daemon / One-click setup
ats agents install            # 安装 hooks / Install hooks
ats agents uninstall          # 移除 hooks / Uninstall hooks
ats agents test -t Read       # 测试 hook handler / Test hook handler
ats agents check              # 健康检查 / Health check
```

**支持的 Agent / Supported Agents：**

| Agent | 环境变量 / Env Var | 配置文件 / Config |
|-------|----------|----------|
| Claude Code | `CLAUDE_CODE` | `~/.claude/settings.local.json` |
| Codex CLI | `OPENAI_CODEX` | `~/.codex/config.json` |
| OpenClaw | `OPENCLAW` | `~/.openclaw/settings.json` |
| Cursor | `CURSOR` | `~/.cursor/settings.json` |
| Continue | `CONTINUE` | `~/.continue/config.json` |
| Windsurf | `WINDSURF` | `~/.windsurf/settings.json` |
| Aider | `AIDER` | `~/.aider/configuration.yml` |

### 3. `hooks` — Hook 管理 / Hook Management

直接管理 Agent Hook 配置（与 `agents` 命令功能重叠，保留兼容）。

Directly manage Agent Hook configuration (overlaps with `agents` commands, kept for compatibility).

```bash
ats hooks install             # 自动检测并安装 / Auto-detect and install
ats hooks uninstall           # 自动检测并移除 / Auto-detect and uninstall
ats hooks status              # 检查所有 Agent 的 hooks 状态 / Check hook status for all agents
ats hooks test -t Read        # 测试 hook 是否生效 / Test if hook works
ats hooks test -t Glob --stdin  # 从 stdin 读取事件测试 / Read event from stdin
```

### 4. `sessions` — 会话管理 / Session Management

追踪和管理 Agent 对话会话，支持自动 compact。

Track and manage Agent conversation sessions with auto-compaction.

```bash
ats sessions list             # 列出所有会话 / List all sessions
ats sessions create <title>   # 创建带主题标签的会话 / Create session with topic tag
ats sessions info <id>        # 查看会话详情和 compact 历史 / View session details
ats sessions compact-log      # 记录 compact 操作 / Log compaction operations
ats sessions topics           # 主题分布统计 / Topic distribution stats
ats sessions stats            # 会话统计概览 / Session statistics overview
```

### 5. `stats` — 统计分析 / Analytics

全面分析 Token 消耗，识别浪费来源。

Comprehensive token consumption analysis, identifying waste sources.

```bash
ats stats report              # 综合报告 / Comprehensive report
ats stats files --limit 20    # 文件读取热力图（Top 20）/ File read heatmap
ats stats trend               # Token 消耗趋势图 / Token consumption trend
ats stats suggest             # 节省建议 / Saving suggestions
ats stats cost --days 30      # 费用估算（最近 30 天）/ Cost estimation (last 30 days)
```

### 6. `transcript` — Transcript 解析 / Transcript Parsing

增量解析 Agent 的 transcript JSONL 文件，提取 usage 和 tool_use 事件。

Incrementally parse Agent transcript JSONL files, extract usage and tool_use events.

```bash
ats transcript scan           # 扫描所有 Agent 的 transcript / Scan all agent transcripts
ats transcript parse <path>   # 解析指定文件 / Parse specific file
ats transcript stats          # Token 消耗历史 / Token consumption history
ats transcript tools          # 工具调用统计 / Tool usage statistics
```

### 7. `daemon` — 后台监控服务 / Background Daemon

后台守护进程，定期扫描 transcript 文件，写入 analytics DB，提供 HTTP API。

Background daemon that periodically scans transcript files, writes to analytics DB, provides HTTP API.

```bash
ats daemon start              # 启动后台服务 / Start background service
ats daemon stop               # 停止服务 / Stop service
ats daemon status             # 查看运行状态 / Check running status
```

**功能 / Features：**

- 增量扫描（基于文件偏移量 + mtime，不重复处理已读内容）/ Incremental scanning (offset + mtime based)
- 批量写入数据库（单事务，减少 I/O）/ Batch DB writes (single transaction)
- 周期性告警（大文件检测、异常消耗提醒）/ Periodic alerts (large file detection)
- HTTP API（`/status` `/sessions` `/alerts`），带 Bearer Token 认证 / HTTP API with Bearer Token auth

### 8. `tui` — 实时仪表盘 / Real-Time Dashboard

终端实时显示 Token 消耗仪表盘。

Real-time token consumption dashboard in terminal.

```bash
ats tui                       # 启动 TUI 仪表盘 / Start TUI dashboard
```

---

## 配置 / Configuration

配置文件：`~/.agent-token-saver/config.yaml`

```yaml
# 模型配置 / Model config
model: claude-sonnet-4-20250514

# 自动 compact 阈值（token 数）/ Auto-compact threshold (tokens)
auto_compact_threshold: 100000

# Compact 保留策略 / Compact retention policy
compact_keep_ratio: 0.3       # 保留最近 30% / Keep recent 30%
compact_keep_recent: 5        # 保留最近 N 轮完整内容 / Keep last N rounds

# 预处理选项 / Preprocessing options
strip_comments: true          # 去除注释 / Strip comments
strip_docstrings: false       # 去除 Python docstring / Strip Python docstrings
max_file_tokens: 50000        # 单文件最大 token 数 / Max tokens per file
max_total_tokens: 200000      # 总 token 预算 / Total token budget

# 默认行为 / Default behavior
auto_detail_default: false    # 自适应 detail_level / Adaptive detail_level
structural_dedup_default: false  # 结构去重 / Structural dedup

# 忽略目录 / Ignored directories
ignore_dirs:
  - .git
  - .svn
  - .hg
  - __pycache__
  - node_modules
  - .venv
  - venv
  - dist
  - build
  - .idea
  - .vscode

# 忽略文件 / Ignored files
ignore_files:
  - .DS_Store
  - Thumbs.db
```

### 修改配置 / Modify Configuration

```bash
ats config --show                        # 显示当前配置 / Show current config
ats config --set max_file_tokens=80000   # 修改配置项 / Modify config item
ats config --reset                       # 重置为默认配置 / Reset to defaults
```

---

## 可移植性 / Portability

### 环境变量覆盖 / Environment Variables

所有路径均可通过环境变量自定义：

All paths can be customized via environment variables:

```bash
# 自定义配置目录 / Custom config directory
export CTS_CONFIG_DIR=/path/to/config/

# 自定义 analytics 数据库 / Custom analytics database
export CTS_ANALYTICS_DB=/path/to/analytics.db
```

### 便携模式 / Portable Mode

不写入 `~/.agent-token-saver/`，使用本地配置：

Use local config without writing to `~/.agent-token-saver/`:

```bash
ats --config ./local_config.yaml prep files src/
```

### Docker 部署 / Docker Deployment

```dockerfile
FROM python:3.12-slim

RUN pip install agent-token-saver

# 作为预处理服务 / As preprocessing service
CMD ["ats", "prep", "pipe"]
```

### NPX 运行 / Run via NPX

```bash
# 无需安装，直接运行 / Run without installation
npx agent-token-saver setup

# 或在 package.json scripts 中使用 / Or use in package.json scripts
npx agent-token-saver prep pipe
```

适合在已有 Node.js 环境的项目中快速使用，或作为 CI 中的一次性工具。

Ideal for quick use in projects with Node.js, or as a one-time CI tool.

### CI/CD 集成 / CI/CD Integration

```yaml
# GitHub Actions — pip 方式 / pip method
- name: Install agent-token-saver
  run: pip install agent-token-saver

- name: One-click setup
  run: ats-setup

# 或使用 npx（无需预装 Python 包）/ Or use npx (no pre-install needed)
- name: Setup via npx
  run: npx agent-token-saver setup
```

---

## 架构 / Architecture

### 项目结构 / Project Structure

```
claude_token_saver/
├── __init__.py
├── cli.py                  # 主 CLI 入口（ats / ats-setup） / Main CLI entry
├── cli_prep.py             # prep 子命令 / prep subcommand
├── cli_sessions.py         # sessions 子命令 / sessions subcommand
├── cli_stats.py            # stats 子命令 / stats subcommand
├── cli_hooks.py            # hooks 子命令 / hooks subcommand
├── cli_agents.py           # agents 子命令 / agents subcommand
├── cli_transcript.py       # transcript 子命令 / transcript subcommand
├── cli_daemon.py           # daemon 子命令 / daemon subcommand
├── cli_tui.py              # tui 子命令 / tui subcommand
├── cli_helpers.py          # CLI 辅助函数 / CLI helpers
├── config.py               # 配置管理 / Configuration management
├── agents/                 # 多 Agent 适配器 / Multi-agent adapters
│   ├── __init__.py         # 注册表 + 事件处理 / Registry + event handling
│   ├── base.py             # GenericJsonAdapter 基类 / Base class
│   ├── claude_code.py      # Claude Code（特殊适配） / Claude Code (special)
│   ├── codex.py            # Codex CLI
│   ├── openclaw.py         # OpenClaw
│   ├── cursor.py           # Cursor
│   ├── aider.py            # Aider（YAML 配置） / Aider (YAML config)
│   ├── continue.py         # Continue
│   └── windsurf.py         # Windsurf
├── prep/                   # 预处理核心（包入口） / Preprocessing core
│   └── __init__.py         # 50+ 压缩优化函数 / 50+ compression functions
├── hooks/                  # Hook 系统 / Hook system
│   ├── __init__.py         # 配置生成、安装、卸载 / Config generation
│   └── handler.py          # Hook 事件处理入口 / Hook event handler
├── sessions/               # 会话管理（SQLite） / Session management
│   └── __init__.py
├── stats/                  # 统计分析 / Analytics
│   └── __init__.py
├── transcript/             # Transcript 解析 / Transcript parsing
│   └── __init__.py
├── daemon/                 # 后台监控服务 / Background daemon
│   └── __init__.py
├── tui/                    # 终端仪表盘 / Terminal dashboard
│   └── __init__.py
├── utils/                  # 工具函数（token 计数、路径优化等） / Utilities
│   └── __init__.py
├── budget.py               # 自适应预算分配 / Adaptive budget allocation
├── compactor.py            # 对话上下文压缩 / Conversation context compaction
├── compression_pipeline.py # 多阶段压缩管线 / Multi-stage compression pipeline
├── compressor.py           # 骨架提取、符号索引 / Skeleton extraction
├── conversation_diff.py    # 对话 Diff 压缩 / Conversation diff compression
├── hook_optimizer.py       # Hook 输出最小化 / Hook output minimization
├── incremental_context.py  # 增量上下文 / Incremental context
├── path_optimizer.py       # 路径缩写 / Path abbreviation
├── progressive.py          # 渐进式披露 / Progressive disclosure
├── simhash_dedup.py        # SimHash 近似去重 / SimHash fuzzy dedup
├── token_budget.py         # Token 预算分配 / Token budget allocation
├── common_dedup.py         # 常见文件去重 / Common file dedup
└── gitignore.py            # .gitignore 感知过滤 / .gitignore aware filtering

tests/                       # 测试套件（441+ 用例） / Test suite (441+ tests)
```

### 适配器架构 / Adapter Architecture

通用 Agent 适配器只需声明类属性，无需重写任何方法（Claude Code / Aider 等特殊 Agent 除外）：

Generic Agent adapters only need to declare class attributes — no method overrides required (except for special agents like Claude Code / Aider):

```python
# agents/codex.py
from claude_token_saver.agents.base import (
    AgentID, GenericJsonAdapter, _register,
)

@_register
class _Codex(GenericJsonAdapter):
    agent_id = AgentID.CODEX
    name = "OpenAI Codex"
    settings_path = Path.home() / ".codex" / "config.json"
    tool_map = {"read_file": "Read", "write_file": "Write", ...}
    env_vars = {"OPENAI_CODEX": "1"}
    event_type_map = {"pre_tool": "PreToolUse", "post_tool": "PostToolUse"}
    outbound_keys = {"allow": "allow", "message": "reason", ...}
```

`_register` 是 `base.py` 中的装饰器函数，将类注册到全局 `_ADAPTER_REGISTRY`。所有适配器模块在 `agents/__init__.py` 中被导入，触发注册。

`_register` is a decorator function in `base.py` that registers the class to the global `_ADAPTER_REGISTRY`. All adapter modules are imported in `agents/__init__.py`, triggering registration.

特殊适配器（Claude Code、Aider）继承 `AgentAdapter` 并重写需要差异化的方法。

Special adapters (Claude Code, Aider) inherit `AgentAdapter` and override methods that need customization.

### 事件处理流程 / Event Processing Flow

```
Agent Hook → handler.py → 适配器.parse_inbound_event() → HookEvent
                                              ↓
                                   handle_pre_tool() / handle_post_tool()
                                              ↓
                                   HookDecision
                                              ↓
                           适配器.format_outbound_decision() → Agent 格式
```

---

## 开发 / Development

```bash
# 安装开发依赖 / Install development dependencies
pip install -e ".[dev]"

# 运行测试 / Run tests
pytest tests/ -v

# 覆盖率报告 / Coverage report
pytest tests/ --cov=claude_token_saver --cov-report=term-missing
```

### 新增 Agent 适配器 / Adding a New Agent Adapter

1. 在 `agents/` 下创建新文件，继承 `GenericJsonAdapter` / Create a new file under `agents/`, inherit from `GenericJsonAdapter`
2. 声明类属性（`agent_id`、`name`、`settings_path` 等）/ Declare class attributes
3. 用 `@_register` 装饰器注册 / Register with `@_register` decorator
4. 在 `agents/__init__.py` 中导入 / Import in `agents/__init__.py`

---

## License

The MIT License (MIT)
Copyright (c) <year> <copyright holders>
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
The Software is provided “as is”, without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the Software.
