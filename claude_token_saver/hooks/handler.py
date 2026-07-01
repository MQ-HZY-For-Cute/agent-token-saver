"""
Agent Token Saver - Hook Handler
处理 Claude Code 的 PreToolUse 和 PostToolUse hooks。

从 stdin 读取 JSON 事件，输出 JSON 决策到 stdout，警告输出到 stderr。
仅使用标准库（json, sys, pathlib, sqlite3）及同包 utils 模块。

Token 节省策略：
  - 大文件读取询问（>500KB 询问用户是否继续，建议使用 --offset/--limit）
  - Glob 结果数量限制（注入 maxResults=100）
  - Grep 结果数量限制（注入 maxMatches=50）
  - 目录排除注入（自动排除 node_modules/.venv/.git 等）
  - Read 结果去重（同一文件短时间重复读取时引用已有结果）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from claude_token_saver.utils import count_tokens, get_file_size

# ── 常量 ──────────────────────────────────────────────────────────────

# 默认大文件阈值（字节），约 200KB
DEFAULT_MAX_FILE_SIZE_BYTES: int = 200_000

# 阻止读取的阈值（字节），约 500KB — 超过此大小的文件直接阻止
BLOCK_MAX_FILE_SIZE_BYTES: int = 500_000

# Glob 最大结果数
DEFAULT_GLOB_MAX_RESULTS: int = 100

# Grep 最大匹配数
DEFAULT_GREP_MAX_MATCHES: int = 50

# 分析数据库路径
ANALYTICS_DB: Path = Path.home() / ".agent-token-saver" / "analytics.db"

# 默认排除目录
DEFAULT_EXCLUDE_DIRS: set[str] = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", "dist", "build", ".idea", ".vscode",
    ".gradle", "target", "bin", "obj",
}

# 默认排除文件
DEFAULT_EXCLUDE_FILES: set[str] = {
    ".DS_Store", "Thumbs.db",
}

# 预计算的排除模式（避免每次 hook 调用时重建）
_GLOB_DEFAULT_EXCLUDES: list[str] = sorted(DEFAULT_EXCLUDE_DIRS)
_GREP_EXCLUDE_PATTERNS: list[str] = (
    [f"*/{d}/**" for d in sorted(DEFAULT_EXCLUDE_DIRS)]
    + [f"**/{f}" for f in sorted(DEFAULT_EXCLUDE_FILES)]
)
_GREP_EXCLUDE_STRING: str = "|".join(_GREP_EXCLUDE_PATTERNS)

# Read 结果去重缓存：file_path -> (content_hash, token_count, mtime)
_READ_CACHE: dict[str, tuple[str, int, float]] = {}


# ── 数据库操作 ────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db(db_path: Path) -> None:
    """初始化 tool_usage 和 waste_log 表（如不存在）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name   TEXT    NOT NULL,
            tool_input  TEXT    DEFAULT '{}',
            tool_output TEXT    DEFAULT '{}',
            file_path   TEXT,
            file_size   INTEGER DEFAULT 0,
            decision    TEXT    DEFAULT 'approve',
            session_id  TEXT,
            timestamp   TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS waste_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            category      TEXT    NOT NULL,
            description   TEXT    NOT NULL,
            tokens_wasted INTEGER DEFAULT 0,
            suggestion    TEXT    DEFAULT '',
            timestamp     TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT    NOT NULL,
            description TEXT    NOT NULL,
            details     TEXT    DEFAULT '',
            timestamp   TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _record_security_event(event_type: str, description: str, details: str = "") -> None:
    """记录安全相关事件到 analytics DB。"""
    _init_db(ANALYTICS_DB)
    conn = sqlite3.connect(ANALYTICS_DB)
    conn.execute(
        "INSERT INTO security_log (event_type, description, details, timestamp) VALUES (?, ?, ?, ?)",
        (event_type, description, details, _utcnow()),
    )
    conn.commit()
    conn.close()


def _record_tool_usage(
    tool_name: str,
    tool_input: dict,
    tool_output: dict | None,
    file_path: str | None,
    file_size: int,
    decision: str,
    session_id: str | None,
) -> None:
    """记录 tool 使用到 analytics DB。"""
    _init_db(ANALYTICS_DB)
    conn = sqlite3.connect(ANALYTICS_DB)
    conn.execute(
        "INSERT INTO tool_usage "
        "(tool_name, tool_input, tool_output, file_path, file_size, decision, session_id, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tool_name,
            json.dumps(tool_input, ensure_ascii=False),
            json.dumps(tool_output or {}, ensure_ascii=False),
            file_path,
            file_size,
            decision,
            session_id,
            _utcnow(),
        ),
    )
    conn.commit()
    conn.close()


# ── Read 结果去重 ──────────────────────────────────────────────────────

def _get_read_cache_key(file_path: str) -> str:
    """生成 Read 缓存的键。"""
    return file_path


def _get_file_content_hash(file_path: str) -> str | None:
    """计算文件内容的 MD5 hash（统一换行符，确保跨平台一致性）。"""
    try:
        # newline=None 启用 universal newlines 模式，自动将 \r\n → \n
        with open(file_path, "r", encoding="utf-8", errors="replace", newline=None) as f:
            content = f.read()
        return hashlib.sha256(content.encode()).hexdigest()
    except Exception:
        return None


def _check_read_cache(file_path: str) -> tuple[bool, int]:
    """检查 Read 缓存：返回 (是否命中, token_count)。"""
    import os
    key = _get_read_cache_key(file_path)
    if key not in _READ_CACHE:
        return False, 0
    cached_hash, cached_tokens, cached_mtime = _READ_CACHE[key]
    # 快速路径：检查 mtime（无需读取文件内容）
    try:
        current_mtime = os.path.getmtime(file_path)
        if current_mtime == cached_mtime:
            return True, cached_tokens
    except OSError:
        pass
    # mtime 变化：回退到完整内容 hash 检查
    current_hash = _get_file_content_hash(file_path)
    if current_hash is None or current_hash != cached_hash:
        return False, 0
    # mtime 变了但内容没变（如 touch 操作），更新缓存
    try:
        _READ_CACHE[key] = (current_hash, cached_tokens, os.path.getmtime(file_path))
    except OSError:
        pass
    return True, cached_tokens


def _update_read_cache(file_path: str, content: str) -> None:
    """更新 Read 缓存。"""
    import os
    try:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        tokens = count_tokens(content)
        mtime = os.path.getmtime(file_path)
        _READ_CACHE[file_path] = (content_hash, tokens, mtime)
    except Exception:
        pass


def clear_read_cache() -> None:
    """清空 Read 缓存。"""
    _READ_CACHE.clear()


# ── 工具函数 ──────────────────────────────────────────────────────────

def _warn_large_file(file_path: str, file_size: int) -> str:
    """生成大文件警告文本。"""
    size_kb = file_size / 1024
    return (
        f"[ats] 文件过大 ({size_kb:.0f} KB)，"
        f"建议使用 --offset 和 --limit 参数只读取需要的部分。"
    )


def _add_exclude_paths_to_input(tool_input: dict, tool_name: str) -> dict:
    """为 Glob/Grep 添加排除路径和结果数量限制。"""
    modified = dict(tool_input)

    if tool_name == "Glob":
        # Glob 工具支持 excludeDirs 参数
        existing_excludes = modified.get("excludeDirs", [])
        if isinstance(existing_excludes, list):
            new_excludes = list(existing_excludes)
            for d in _GLOB_DEFAULT_EXCLUDES:
                if d not in new_excludes:
                    new_excludes.append(d)
            modified["excludeDirs"] = new_excludes

        # 限制结果数量（如果未设置）
        if "maxResults" not in modified:
            modified["maxResults"] = DEFAULT_GLOB_MAX_RESULTS

    elif tool_name == "Grep":
        # Grep 工具支持 exclude 参数（管道分隔的 glob 模式）
        existing_exclude = modified.get("exclude", "")
        if existing_exclude:
            modified["exclude"] = existing_exclude + "|" + _GREP_EXCLUDE_STRING
        else:
            modified["exclude"] = _GREP_EXCLUDE_STRING

        # 限制匹配数量（如果未设置）
        if "maxMatches" not in modified:
            modified["maxMatches"] = DEFAULT_GREP_MAX_MATCHES

    return modified


# ── 事件处理 ──────────────────────────────────────────────────────────

def _safe_resolve_path(file_path: str) -> str | None:
    """安全规范化路径：防止路径遍历，只允许在工作目录范围内。"""
    if not file_path:
        return None
    # 拒绝包含空字节的路径（Windows 上 Path.resolve() 不对此报错）
    if "\x00" in file_path:
        _record_security_event(
            "path_traversal_attempt",
            f"路径遍历尝试被阻止（空字节）: {file_path[:50]}",
            f"cwd={Path.cwd()}",
        )
        return None
    resolved = None
    try:
        resolved = Path(file_path).resolve()
        cwd = Path.cwd().resolve()
        resolved.relative_to(cwd)
    except (OSError, ValueError):
        _record_security_event(
            "path_traversal_attempt",
            f"路径遍历尝试被阻止: {file_path}",
            f"cwd={Path.cwd()}, resolved={resolved}",
        )
        return None
    return str(resolved)


def handle_pre_tool(tool_name: str, tool_input: dict, session_id: str | None = None) -> dict:
    """处理 PreToolUse 事件。

    - Read: 检查文件大小，过大则询问用户是否继续
    - Glob/Grep: 自动添加排除路径和结果数量限制
    """
    modified_input = dict(tool_input)
    decision = "approve"
    file_path: str | None = None
    safe_path: str | None = None
    file_size: int = 0
    reason = ""

    if tool_name == "Read":
        raw_path = tool_input.get("file_path", "")
        safe_path = _safe_resolve_path(raw_path)
        if safe_path:
            path = Path(safe_path)
            if path.exists():
                try:
                    file_size = get_file_size(path)
                    if file_size > BLOCK_MAX_FILE_SIZE_BYTES:
                        # 超大文件：询问用户是否继续
                        decision = "ask"
                        reason = (
                            f"[ats] 文件过大 ({file_size / 1024:.0f} KB)，"
                            f"建议使用 --offset 和 --limit 参数只读取需要的部分。是否继续？"
                        )
                    elif file_size > DEFAULT_MAX_FILE_SIZE_BYTES:
                        # 大文件：警告但允许
                        reason = _warn_large_file(safe_path, file_size)

                    # Read 结果去重：文件未变更时提示 Claude 使用上次结果
                    if decision == "approve" and not reason:
                        cache_hit, cached_tokens = _check_read_cache(safe_path)
                        if cache_hit:
                            reason = (
                                f"[ats] 此文件内容未变更（上次读取 {cached_tokens:,} tokens），"
                                f"可复用之前的结果，无需重新读取。"
                            )
                except OSError:
                    pass
        file_path = safe_path or ""

    elif tool_name in ("Glob", "Grep"):
        raw_path = tool_input.get("file_path", "")
        safe_path = _safe_resolve_path(raw_path) if raw_path else None
        modified_input = _add_exclude_paths_to_input(tool_input, tool_name)

    result: dict = {"decision": decision, "reason": reason}

    if modified_input != tool_input:
        result["modified_input"] = modified_input

    _record_tool_usage(tool_name, tool_input, None, file_path, file_size, decision, session_id)

    # 压缩 reason 文本以减少 token 消耗
    try:
        if result.get("reason"):
            from claude_token_saver.hook_optimizer import compress_reason_text
            result["reason"] = compress_reason_text(result["reason"])
    except Exception:
        pass

    return result


def handle_post_tool(
    tool_name: str,
    tool_input: dict,
    tool_output: dict,
    session_id: str | None = None,
) -> dict:
    """处理 PostToolUse 事件。

    - 记录所有 tool 使用到 analytics DB
    - 大文件读取记录浪费
    """
    file_path: str | None = None
    safe_path: str | None = None
    file_size: int = 0

    if tool_name == "Read":
        raw_path = tool_input.get("file_path", "")
        safe_path = _safe_resolve_path(raw_path)
        if safe_path:
            path = Path(safe_path)
            if path.exists():
                try:
                    file_size = get_file_size(path)
                except OSError:
                    pass

    # 记录大文件读取浪费
    if tool_name == "Read" and safe_path and file_size > DEFAULT_MAX_FILE_SIZE_BYTES:
        _init_db(ANALYTICS_DB)
        conn = sqlite3.connect(ANALYTICS_DB)
        tokens_wasted = file_size // 4
        conn.execute(
            "INSERT INTO waste_log "
            "(category, description, tokens_wasted, suggestion, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "大文件读取",
                f"读取大文件: {safe_path} ({file_size / 1024:.1f} KB)",
                tokens_wasted,
                "使用 --offset 和 --limit 参数只读取需要的部分",
                _utcnow(),
            ),
        )
        conn.commit()
        conn.close()

    _record_tool_usage(tool_name, tool_input, tool_output, safe_path, file_size, "approve", session_id)

    # ── Read 结果缓存更新 ─────────────────────────────────────────────────
    if tool_name == "Read" and safe_path:
        try:
            # 优先使用 tool_output 中的内容，避免重复 I/O
            output_content = ""
            if isinstance(tool_output, dict):
                output_content = tool_output.get("content", "")
            elif isinstance(tool_output, str):
                output_content = tool_output
            if output_content:
                _update_read_cache(safe_path, output_content)
            else:
                # 回退：从文件读取
                content = Path(safe_path).read_text(encoding="utf-8", errors="replace")
                _update_read_cache(safe_path, content)
        except Exception:
            pass

    # ── 工具输出压缩 ─────────────────────────────────────────────────────
    compressed_output = _compress_tool_output(tool_name, tool_input, tool_output)
    if compressed_output is not tool_output:
        return {"decision": "approve", "reason": "", "modified_output": compressed_output}

    return {"decision": "approve", "reason": ""}


# ── 工具输出压缩 ────────────────────────────────────────────────────────

# Grep 单次匹配最大上下文行数
DEFAULT_GREP_CONTEXT_LINES: int = 2

# Glob 结果最大展示数（超出后只列路径）
DEFAULT_GLOB_DISPLAY_LIMIT: int = 30


def _compress_tool_output(
    tool_name: str,
    tool_input: dict,
    tool_output: dict,
) -> dict:
    """压缩工具输出，减少返回给 Claude 的 token。

    - Grep: 限制每匹配的上下文行数（默认 2 行）
    - Glob: 结果过多时只返回路径列表，省略文件预览
    """
    if tool_name == "Grep":
        return _compress_grep_output(tool_output)
    elif tool_name == "Glob":
        return _compress_glob_output(tool_output)
    return tool_output


def _compress_grep_output(output: dict) -> dict:
    """压缩 Grep 输出：限制上下文行数，多匹配时剥离 content 字段。"""
    if not isinstance(output, dict):
        return output

    matches = output.get("matches", [])
    if not isinstance(matches, list):
        return output

    max_context = DEFAULT_GREP_CONTEXT_LINES
    compressed = []
    many_matches = len(matches) > 20

    for match in matches:
        if not isinstance(match, dict):
            compressed.append(match)
            continue

        context = match.get("context", [])
        needs_truncate = isinstance(context, list) and len(context) > max_context * 2 + 1
        needs_strip = many_matches

        if not needs_truncate and not needs_strip:
            compressed.append(match)
            continue

        compressed_match = dict(match)
        if needs_truncate:
            center = len(context) // 2
            start = max(0, center - max_context)
            end = min(len(context), center + max_context + 1)
            compressed_match["context"] = context[start:end]
            compressed_match["context_truncated"] = True

        if needs_strip:
            for key in ("content", "context", "context_truncated"):
                compressed_match.pop(key, None)

        compressed.append(compressed_match)

    result = dict(output)
    result["matches"] = compressed
    return result


def _compress_glob_output(output: dict) -> dict:
    """压缩 Glob 输出：结果过多时省略内容预览。"""
    if not isinstance(output, dict):
        return output

    results = output.get("results", [])
    if not isinstance(results, list):
        return output

    if len(results) > DEFAULT_GLOB_DISPLAY_LIMIT:
        # 只保留路径，移除大文件的内容预览
        compressed = []
        for r in results:
            if not isinstance(r, dict):
                compressed.append(r)
                continue
            cr = {k: v for k, v in r.items() if k in ("path", "is_dir", "size")}
            compressed.append(cr)
        result = dict(output)
        result["results"] = compressed
        result["truncated"] = True
        result["total_results"] = len(results)
        return result

    return output


# ── 主入口 ────────────────────────────────────────────────────────────

def _detect_format(event: dict) -> str:
    """根据事件字段检测 Agent 格式。

    Returns:
        "claude" | "codex" | "openclaw" | "cursor" | "aider" | "continue" | "windsurf" | "unknown"
    """
    # Claude Code: 使用 hook_event_name 和 tool_name
    if "hook_event_name" in event or "tool_name" in event:
        return "claude"
    # Aider: 使用 event 字段
    if "event" in event and event.get("event") in ("pre_tool", "post_tool"):
        return "aider"
    # Continue: 使用 camelCase type 值
    if event.get("type") in ("preToolUse", "postToolUse"):
        return "continue"
    # Cursor / Windsurf / OpenClaw / Codex: type + tool (统一格式)
    if "type" in event and "tool" in event:
        return "generic_tool"
    return "unknown"


def _get_adapter_for_format(fmt: str, raw: Optional[dict] = None) -> Any:
    """根据格式字符串获取对应的适配器。

    对于通用格式 (generic_tool)，尝试通过事件内容匹配适配器。
    """
    try:
        from claude_token_saver.agents import get_adapter
        from claude_token_saver.agents.base import AgentID

        fmt_to_id = {
            "claude": AgentID.CLAUDE_CODE,
            "codex": AgentID.CODEX,
            "openclaw": AgentID.OPENCLAW,
            "cursor": AgentID.CURSOR,
            "aider": AgentID.AIDER,
            "continue": AgentID.CONTINUE,
            "windsurf": AgentID.WINDSURF,
        }
        aid = fmt_to_id.get(fmt)
        if aid:
            return get_adapter(aid)

        # generic_tool: 通过事件内容匹配适配器
        if fmt == "generic_tool" and raw:
            return _match_adapter_by_content(raw)
    except Exception:
        pass
    return None


def _match_adapter_by_content(raw: dict) -> Any:
    """通过事件内容尝试匹配适配器。

    对于 Codex/OpenClaw/Cursor/Windsurf 等使用相同格式的 Agent，
    通过尝试解析来找到正确的适配器。
    """
    try:
        from claude_token_saver.agents import get_adapter
        from claude_token_saver.agents.base import AgentID

        candidates = [
            AgentID.CODEX,
            AgentID.OPENCLAW,
            AgentID.CURSOR,
            AgentID.WINDSURF,
        ]
        for aid in candidates:
            adapter = get_adapter(aid)
            if adapter is None:
                continue
            try:
                event = adapter.parse_inbound_event(raw)
                # 成功解析且有工具名 → 认为是匹配的
                if event.tool_name:
                    return adapter
            except Exception:
                continue
    except Exception:
        pass
    return None


def main() -> None:
    """从 stdin 读取 JSON 事件，处理并输出 JSON 结果到 stdout。

    支持多 Agent 格式自动检测：
      - Claude Code (hook_event_name / tool_name)
      - Codex CLI (type / tool)
      - OpenClaw (type / tool)
      - Cursor (type / tool)
      - Aider (event / tool)
      - Continue (type in camelCase)
      - Windsurf (type / tool)
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"decision": "approve", "reason": ""}), file=sys.stdout)
            sys.exit(0)

        event = json.loads(raw)
    except (json.JSONDecodeError, IOError, UnicodeDecodeError):
        print(json.dumps({"decision": "approve", "reason": ""}), file=sys.stdout)
        sys.exit(0)

    # 检测 Agent 格式并选择适配器
    fmt = _detect_format(event)
    adapter = _get_adapter_for_format(fmt, event)

    if adapter:
        try:
            from claude_token_saver.agents import process_event
            result = process_event(event, adapter)
            print(json.dumps(result), file=sys.stdout)
            return
        except Exception as e:
            _record_security_event("handler_error", f"适配器处理异常: {type(e).__name__}", str(e)[:500])

    # 回退：兼容旧版 Claude Code 格式（无适配器时）
    from claude_token_saver.agents import process_event_direct
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    tool_output = event.get("tool_output") or {}
    session_id = event.get("session_id")
    event_type = event.get("hook_event_name", "")

    if not tool_name:
        print(json.dumps({"decision": "approve", "reason": "no tool_name"}), file=sys.stdout)
        sys.exit(0)

    try:
        result = process_event_direct(tool_name, tool_input, tool_output, session_id, event_type)
    except Exception as e:
        _record_security_event("handler_error", f"Hook handler 异常: {type(e).__name__}", str(e)[:500])
        result = {"decision": "approve", "reason": ""}

    print(json.dumps(result), file=sys.stdout)


if __name__ == "__main__":
    main()
