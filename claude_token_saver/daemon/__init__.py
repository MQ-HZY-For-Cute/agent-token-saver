"""
Agent Token Saver - Daemon 监控服务

后台扫描所有 Agent 的 transcript JSONL，解析 usage 和 tool_use 事件，
写入 analytics DB，并提供轻量 HTTP API。

标准库依赖：threading, http.server, json, time, pathlib, sqlite3, signal
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import urllib.request
from urllib.parse import parse_qs
from datetime import datetime, timezone
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# 路径常量
# ═══════════════════════════════════════════════════════════════════════════════

DAEMON_DIR = Path.home() / ".agent-token-saver"
PID_FILE = DAEMON_DIR / "daemon.pid"
LOG_FILE = DAEMON_DIR / "daemon.log"
DB_PATH = DAEMON_DIR / "daemon_analytics.db"
SCAN_INTERVAL = 30  # 扫描间隔（秒）
HTTP_PORT = 17890  # HTTP API 端口
API_TOKEN_FILE = DAEMON_DIR / ".api_token"  # API 认证 token 文件


def _get_agent_transcript_dirs() -> list[Path]:
    """返回所有已注册 Agent 的 transcript 目录。"""
    try:
        from claude_token_saver.agents import get_all_adapters
        dirs = []
        for adapter in get_all_adapters():
            d = adapter.get_transcript_dir()
            if d and d.exists():
                dirs.append(d)
        return dirs
    except Exception:
        # 回退：仅扫描 Claude Code
        claude_dir = Path.home() / ".claude" / "projects"
        return [claude_dir] if claude_dir.exists() else []


def _get_agent_project_dirs() -> list[Path]:
    """返回所有已注册 Agent 的 project 目录。"""
    try:
        from claude_token_saver.agents import get_all_adapters
        dirs = []
        for adapter in get_all_adapters():
            d = adapter.get_project_dir()
            if d and d.exists():
                dirs.append(d)
        return dirs
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# 日志配置
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("claude_token_saver.daemon")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)

    # 文件 handler（追加模式）
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 控制台 handler（仅 INFO 及以上）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = _setup_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# 数据库初始化
# ═══════════════════════════════════════════════════════════════════════════════

def _init_db() -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parsed_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    NOT NULL,
            event_type      TEXT    NOT NULL,
            model           TEXT    DEFAULT '',
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens     INTEGER DEFAULT 0,
            tool_name       TEXT    DEFAULT '',
            tool_input      TEXT    DEFAULT '',
            tool_output_preview TEXT DEFAULT '',
            file_path       TEXT    DEFAULT '',
            cwd             TEXT    DEFAULT '',
            timestamp       TEXT    NOT NULL,
            parsed_at       TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            level       TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            data        TEXT    DEFAULT '{}',
            timestamp   TEXT    NOT NULL,
            acknowledged INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_state (
            file_path    TEXT PRIMARY KEY,
            last_offset  INTEGER DEFAULT 0,
            last_mtime   REAL    DEFAULT 0,
            updated_at   TEXT    NOT NULL
        )
    """)
    # 索引：加速 session 查询
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_session ON parsed_events(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON parsed_events(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp)"
    )
    conn.commit()
    conn.close()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_db_initialized = False


def _ensure_db() -> None:
    """懒初始化数据库（幂等：多次调用只执行一次）。"""
    global _db_initialized
    if not _db_initialized:
        _init_db()
        _db_initialized = True


# ═══════════════════════════════════════════════════════════════════════════════
# 数据库写入辅助
# ═══════════════════════════════════════════════════════════════════════════════

def _record_event(event: dict[str, Any]) -> None:
    _ensure_db()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO parsed_events
           (session_id, event_type, model, input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens, tool_name, tool_input,
            tool_output_preview, file_path, cwd, timestamp, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.get("session_id", ""),
            event.get("event_type", ""),
            event.get("model", ""),
            event.get("input_tokens", 0) or 0,
            event.get("output_tokens", 0) or 0,
            event.get("cache_creation_tokens", 0) or 0,
            event.get("cache_read_tokens", 0) or 0,
            event.get("tool_name", ""),
            event.get("tool_input", "")[:1000],
            event.get("tool_output_preview", "")[:500],
            event.get("file_path", ""),
            event.get("cwd", ""),
            event.get("timestamp", ""),
            _utcnow(),
        ),
    )
    conn.commit()
    conn.close()


def _batch_record_events(events: list[dict[str, Any]]) -> None:
    """批量插入事件到 parsed_events 表（单事务，减少 I/O 开销）。"""
    if not events:
        return
    _ensure_db()
    now = _utcnow()
    rows = [
        (
            e.get("session_id", ""),
            e.get("event_type", ""),
            e.get("model", ""),
            e.get("input_tokens", 0) or 0,
            e.get("output_tokens", 0) or 0,
            e.get("cache_creation_tokens", 0) or 0,
            e.get("cache_read_tokens", 0) or 0,
            e.get("tool_name", ""),
            (e.get("tool_input") or "")[:1000],
            (e.get("tool_output_preview") or "")[:500],
            e.get("file_path", ""),
            e.get("cwd", ""),
            e.get("timestamp", ""),
            now,
        )
        for e in events
    ]
    conn = _get_conn()
    conn.executemany(
        """INSERT INTO parsed_events
           (session_id, event_type, model, input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens, tool_name, tool_input,
            tool_output_preview, file_path, cwd, timestamp, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()


def _record_alert(level: str, category: str, message: str,
                  data: dict[str, Any] | None = None) -> None:
    _ensure_db()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO alerts (level, category, message, data, timestamp) VALUES (?, ?, ?, ?, ?)",
        (level, category, message, json.dumps(data or {}, ensure_ascii=False), _utcnow()),
    )
    conn.commit()
    conn.close()


def _get_scan_state(file_path: str) -> dict[str, Any]:
    _ensure_db()
    conn = _get_conn()
    row = conn.execute(
        "SELECT last_offset, last_mtime FROM scan_state WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    conn.close()
    if row:
        return {"last_offset": row[0], "last_mtime": row[1]}
    return {"last_offset": 0, "last_mtime": 0.0}


def _update_scan_state(file_path: str, offset: int, mtime: float) -> None:
    _ensure_db()
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO scan_state (file_path, last_offset, last_mtime, updated_at)
           VALUES (?, ?, ?, ?)""",
        (file_path, offset, mtime, _utcnow()),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 解析逻辑
# ═══════════════════════════════════════════════════════════════════════════════

def parse_transcript_line(line: str) -> dict[str, Any] | None:
    """解析单行 JSONL，提取 usage 和 tool_use 信息。

    只关注 type 为 assistant 且包含 usage 的事件。
    Returns:
        事件字典，如果无法解析则返回 None
    """
    try:
        entry = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None

    if entry.get("type") != "assistant":
        return None

    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return None

    usage = msg.get("usage", {})
    if not usage:
        return None

    # 基础事件信息
    result: dict[str, Any] = {
        "session_id":       entry.get("sessionId", ""),
        "event_type":       "assistant",
        "model":            msg.get("model", ""),
        "input_tokens":     usage.get("input_tokens", 0) or 0,
        "output_tokens":    usage.get("output_tokens", 0) or 0,
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens":     usage.get("cache_read_input_tokens", 0) or 0,
        "tool_name":        "",
        "tool_input":       "",
        "tool_output_preview": "",
        "file_path":        "",
        "cwd":              entry.get("cwd", ""),
        "timestamp":        entry.get("timestamp", ""),
    }

    # 提取 tool_use
    content = msg.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                result["tool_name"] = block.get("name", "")
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    result["tool_input"] = json.dumps(inp, ensure_ascii=False)[:1000]
                    if "file_path" in inp:
                        result["file_path"] = str(inp["file_path"])
                break  # 只取第一个 tool_use

    return result


def parse_transcript(file_path: Path) -> tuple[int, int]:
    """增量解析一个 transcript JSONL 文件，将有效事件写入 analytics DB。

    基于文件偏移量和 mtime 做增量解析，避免重复处理已读内容。
    如果文件被截断或重写，则从头重新解析。
    批量写入数据库（单事务），减少 I/O 开销。

    Args:
        file_path: transcript JSONL 文件路径

    Returns:
        (parsed_count, skipped_count) — 本文件本次解析和跳过的行数
    """
    try:
        stat = file_path.stat()
    except OSError as e:
        log.warning("无法 stat 文件 %s: %s", file_path, e)
        return (0, 0)

    size = stat.st_size
    mtime = stat.st_mtime

    state = _get_scan_state(str(file_path))

    # 文件变小或 mtime 回退 → 视为新文件，从头解析
    if size < state["last_offset"] or mtime < state["last_mtime"]:
        offset = 0
    else:
        offset = state["last_offset"]

    if offset >= size:
        return (0, 0)  # 无新内容

    parsed = 0
    skipped = 0
    _BATCH_CHUNK = 500  # 每批最多写入事件数，控制内存
    batch: list[dict[str, Any]] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = parse_transcript_line(line)
                if event:
                    event["source_file"] = str(file_path)
                    batch.append(event)
                    parsed += 1
                    if len(batch) >= _BATCH_CHUNK:
                        _batch_record_events(batch)
                        batch = []
                else:
                    skipped += 1
            new_offset = f.tell()
    except (OSError, IOError) as e:
        log.warning("读取文件失败 %s: %s", file_path, e)
        return (0, 0)

    # 批量写入数据库（剩余未满批次的）
    if batch:
        try:
            _batch_record_events(batch)
        except Exception as e:
            log.error("写入事件失败 %s: %s", file_path, e)

    _update_scan_state(str(file_path), new_offset, mtime)
    return (parsed, skipped)


# ═══════════════════════════════════════════════════════════════════════════════
# 扫描线程
# ═══════════════════════════════════════════════════════════════════════════════

def _discover_jsonl_files() -> list[Path]:
    """递归发现所有 Agent 的 transcript JSONL 文件，排除 subagents。"""
    dirs = _get_agent_transcript_dirs()
    files: list[Path] = []
    for d in dirs:
        try:
            for f in d.rglob("*.jsonl"):
                if "subagents" in f.parts:
                    continue
                files.append(f)
        except OSError:
            continue
    return sorted(files, key=lambda p: str(p))


class ScannerThread(threading.Thread):
    """定期扫描 transcript 文件的守护线程。"""

    def __init__(self, scan_interval: int = SCAN_INTERVAL):
        super().__init__(daemon=True, name="cts-scanner")
        self.scan_interval = scan_interval
        self._stop_event = threading.Event()
        self._last_parsed = 0

    def run(self) -> None:
        log.info("扫描线程启动，间隔 %d 秒", self.scan_interval)
        # 首次立即扫描
        self._do_scan()
        while not self._stop_event.wait(self.scan_interval):
            self._do_scan()
        log.info("扫描线程已停止")

    def stop(self) -> None:
        log.debug("通知扫描线程停止")
        self._stop_event.set()

    def _do_scan(self) -> None:
        try:
            files = _discover_jsonl_files()
        except Exception as e:
            log.error("枚举 transcript 文件失败: %s", e)
            return

        total_parsed = 0
        total_skipped = 0
        for fp in files:
            p, s = parse_transcript(fp)
            total_parsed += p
            total_skipped += s

        self._last_parsed = total_parsed
        if total_parsed > 0:
            log.info(
                "扫描完成: +%d 条事件, 跳过 %d 条, 追踪 %d 个文件",
                total_parsed, total_skipped, len(files),
            )

        # 周期性告警检查
        self._check_alerts(files)

    def _check_alerts(self, files: list[Path]) -> None:
        """检查可能需要注意的情况。"""
        for fp in files:
            try:
                size = fp.stat().st_size
                if size > 10 * 1024 * 1024:
                    _record_alert(
                        "warning", "large_session",
                        f"会话文件过大: {fp.name} ({size // 1024 // 1024}MB)",
                        {"file": str(fp), "size_mb": size // 1024 // 1024},
                    )
            except OSError:
                continue


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP API 服务器
# ═══════════════════════════════════════════════════════════════════════════════

class _DaemonRequestHandler(BaseHTTPRequestHandler):
    """Daemon HTTP API 请求处理器。

    端点:
      GET /status  — Daemon 运行状态
      GET /sessions — 已解析的会话列表（按 token 聚合）
      GET /alerts  — 告警列表
    """

    server_ref: "TokenDaemon | None" = None  # 由 _run_http_server 设置

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("[HTTP %d] %s", os.getpid(), fmt % args)

    # ── 响应辅助 ──

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    # ── 路由 ──

    def do_GET(self) -> None:
        # API Token 认证（支持 Authorization: Bearer 或 ?token= 查询参数）
        query_params = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        query_token = query_params.get("token", [""])[0]

        auth_header = self.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "", 1) or query_token
        if not _verify_api_token(token):
            self._err(401, "Unauthorized: missing or invalid API token")
            return

        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/status":
            self._route_status()
        elif path == "/sessions":
            self._route_sessions()
        elif path == "/alerts":
            self._route_alerts()
        else:
            self._err(404, f"Not found: {path}")

    def _route_status(self) -> None:
        daemon = self.__class__.server_ref
        if daemon is None:
            self._err(503, "Daemon not initialized")
            return

        scanner = daemon.scanner
        uptime = int(time.time() - daemon.start_time) if daemon.start_time else 0

        # 从数据库补充统计
        try:
            conn = _get_conn()
            total_events = conn.execute(
                "SELECT COUNT(*) FROM parsed_events"
            ).fetchone()[0]
            total_sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM parsed_events"
            ).fetchone()[0]
            total_alerts = conn.execute(
                "SELECT COUNT(*) FROM alerts"
            ).fetchone()[0]
            conn.close()
        except Exception:
            total_events = 0
            total_sessions = 0
            total_alerts = 0

        self._json(200, {
            "status": "running" if (scanner and scanner.is_alive()) else "stopped",
            "pid": os.getpid(),
            "uptime_seconds": uptime,
            "scan_interval": daemon.scan_interval,
            "http_port": daemon.http_port,
            "scanner_alive": scanner.is_alive() if scanner else False,
            "transcripts_dir": ", ".join(str(d) for d in _get_agent_transcript_dirs()),
            "db_path": str(DB_PATH),
            "total_events": total_events,
            "total_sessions": total_sessions,
            "total_alerts": total_alerts,
            "timestamp": _utcnow(),
        })

    def _route_sessions(self) -> None:
        try:
            conn = _get_conn()
            rows = conn.execute("""
                SELECT session_id,
                       MAX(model) as model,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cache_creation_tokens) as total_cache_creation,
                       SUM(cache_read_tokens) as total_cache_read,
                       COUNT(*) as event_count,
                       MIN(timestamp) as first_seen,
                       MAX(timestamp) as last_seen
                FROM parsed_events
                GROUP BY session_id
                ORDER BY last_seen DESC
                LIMIT 50
            """).fetchall()
            conn.close()
        except Exception:
            rows = []

        sessions = []
        for r in rows:
            sessions.append({
                "session_id": r[0],
                "model": r[1] or "",
                "input_tokens": r[2] or 0,
                "output_tokens": r[3] or 0,
                "cache_creation_tokens": r[4] or 0,
                "cache_read_tokens": r[5] or 0,
                "event_count": r[6],
                "first_seen": r[7],
                "last_seen": r[8],
                "total_tokens": (r[2] or 0) + (r[3] or 0),
            })

        self._json(200, {
            "sessions": sessions,
            "count": len(sessions),
        })

    def _route_alerts(self) -> None:
        try:
            conn = _get_conn()
            rows = conn.execute("""
                SELECT id, level, category, message, data, timestamp, acknowledged
                FROM alerts
                ORDER BY timestamp DESC
                LIMIT 50
            """).fetchall()
            conn.close()
        except Exception:
            rows = []

        alerts = []
        for r in rows:
            try:
                data = json.loads(r[4]) if r[4] else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            alerts.append({
                "id": r[0],
                "level": r[1],
                "category": r[2],
                "message": r[3],
                "data": data,
                "timestamp": r[5],
                "acknowledged": bool(r[6]),
            })

        self._json(200, {
            "alerts": alerts,
            "count": len(alerts),
        })


def _run_http_server(port: int, daemon: "TokenDaemon") -> None:
    """在独立线程中运行 HTTP 服务器（阻塞）。"""
    _DaemonRequestHandler.server_ref = daemon
    server = HTTPServer(("127.0.0.1", port), _DaemonRequestHandler)
    server.timeout = 1  # handle_request 超时，允许检查 stop 事件
    log.info("HTTP API 已启动: http://127.0.0.1:%d", port)

    while not daemon._http_stop_event.is_set():
        server.handle_request()

    server.server_close()
    log.info("HTTP API 已停止")


# ═══════════════════════════════════════════════════════════════════════════════
# TokenDaemon 主类
# ═══════════════════════════════════════════════════════════════════════════════

class TokenDaemon:
    """Claude Token Saver Daemon 主类。

    管理后台扫描线程和 HTTP API 服务器的生命周期。
    """

    def __init__(
        self,
        scan_interval: int = SCAN_INTERVAL,
        http_port: int = HTTP_PORT,
        pid_file: Path | None = None,
    ):
        self.scan_interval = scan_interval
        self.http_port = http_port
        self.pid_file: Path = pid_file or PID_FILE
        self.scanner: ScannerThread | None = None
        self.http_thread: threading.Thread | None = None
        self.start_time: float = 0.0
        self._http_stop_event = threading.Event()

    # ── 生命周期 ──

    def start(self) -> bool:
        """启动 daemon。

        会写入 PID 文件，启动扫描线程和 HTTP 服务器，
        然后阻塞直到收到信号或调用 stop()。

        Returns:
            True 表示成功启动
        """
        if _is_pid_alive(self._read_pid()):
            log.warning("Daemon 已在运行 (PID: %s)", self._read_pid())
            return False

        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        self._write_pid()
        self.start_time = time.time()
        self._setup_signal_handlers()

        # 确保 API token 存在
        if not _get_api_token():
            token = _generate_api_token()
            log.info("API token 已生成: %s...%s", token[:8], token[-4:])

        # 启动扫描器（守护线程）
        self.scanner = ScannerThread(self.scan_interval)
        self.scanner.start()
        log.info("扫描线程已启动 (interval=%ds)", self.scan_interval)

        # 启动 HTTP API（非阻塞线程）
        self.http_thread = threading.Thread(
            target=_run_http_server,
            args=(self.http_port, self),
            daemon=True,
            name="cts-http",
        )
        self.http_thread.start()

        log.info(
            "TokenDaemon 已启动 PID=%d, HTTP=127.0.0.1:%d",
            os.getpid(), self.http_port,
        )
        return True

    def run_forever(self) -> None:
        """阻塞主线程，直到收到停止信号。

        通常在 start() 之后调用：
          daemon.start()
          daemon.run_forever()
        """
        try:
            while not self._http_stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("收到键盘中断")
        finally:
            self._cleanup()

    def stop(self) -> bool:
        """停止 daemon。

        Returns:
            True 表示成功停止
        """
        pid = self._read_pid()
        if pid != os.getpid():
            log.debug("PID 不匹配 (当前 %d, 记录 %s)，继续清理", os.getpid(), pid)
        self._cleanup()
        log.info("TokenDaemon 已停止")
        return True

    # ── PID 管理 ──

    def _write_pid(self) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        log.debug("PID 文件已写入: %s", self.pid_file)

    def _read_pid(self) -> int | None:
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def _remove_pid(self) -> None:
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    # ── 信号处理 ──

    def _setup_signal_handlers(self) -> None:
        def _on_signal(signum: int, _frame: Any) -> None:
            sig_name = signal.Signals(signum).name
            log.info("收到信号 %s，正在停止 daemon...", sig_name)
            self._cleanup()
            sys.exit(0)

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

    # ── 清理 ──

    def _cleanup(self) -> None:
        log.debug("清理 daemon 资源")
        self._http_stop_event.set()
        if self.scanner:
            self.scanner.stop()
            self.scanner.join(timeout=5)
            log.debug("扫描线程已加入")
        if self.http_thread:
            self.http_thread.join(timeout=5)
            log.debug("HTTP 线程已加入")
        self._remove_pid()


# ═══════════════════════════════════════════════════════════════════════════════
# 进程辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

def _is_pid_alive(pid: int | None) -> bool:
    """检查 PID 对应的进程是否存活。"""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _force_kill(pid: int) -> None:
    """强制终止进程（跨平台：Windows taskkill / Unix kill -9）。"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if str(pid) not in result.stdout:
                log.warning("PID %d 不存在或不属于当前用户，跳过终止", pid)
                return
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            # Unix: 先验证进程存在，再 SIGKILL
            os.kill(pid, 0)  # raises ProcessLookupError if not exists
            os.kill(pid, 9)
        log.info("已强制终止 PID %d", pid)
    except (ProcessLookupError, PermissionError):
        log.warning("PID %d 不存在或无权限，跳过终止", pid)
    except Exception as e:
        log.error("强制终止 PID %d 失败: %s", pid, e)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# API Token 管理
# ═══════════════════════════════════════════════════════════════════════════════

_API_TOKEN_CACHE: str | None = None


def _generate_api_token() -> str:
    """生成随机 API token 并保存到文件。"""
    token = secrets.token_urlsafe(32)
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    API_TOKEN_FILE.write_text(token, encoding="utf-8")
    _restrict_token_file()
    _invalidate_api_token_cache()
    return token


def _restrict_token_file() -> None:
    """限制 token 文件权限：Unix 用 chmod，Windows 用 icacls。"""
    try:
        username = os.environ.get("USERNAME", os.environ.get("USER", ""))
        if sys.platform == "win32" and username:
            subprocess.run(
                ["icacls", str(API_TOKEN_FILE), "/inheritance:r",
                 "/grant:r", f"{username}:(R)"],
                capture_output=True, timeout=5,
            )
        else:
            API_TOKEN_FILE.chmod(0o600)
    except Exception:
        pass  # 权限限制失败不影响功能


def _invalidate_api_token_cache() -> None:
    global _API_TOKEN_CACHE
    _API_TOKEN_CACHE = None


def _get_api_token() -> str | None:
    """从文件读取 API token（带内存缓存）。"""
    global _API_TOKEN_CACHE
    if _API_TOKEN_CACHE is not None:
        return _API_TOKEN_CACHE
    if not API_TOKEN_FILE.exists():
        return None
    try:
        _API_TOKEN_CACHE = API_TOKEN_FILE.read_text(encoding="utf-8").strip()
        return _API_TOKEN_CACHE
    except OSError:
        return None


def _verify_api_token(token: str | None) -> bool:
    """验证 API token 是否匹配。"""
    expected = _get_api_token()
    if not expected or not token:
        return False
    return secrets.compare_digest(expected, token)


# ═══════════════════════════════════════════════════════════════════════════════
# 模块级公共 API
# ═══════════════════════════════════════════════════════════════════════════════

def _run_detached(
    scan_interval: int = SCAN_INTERVAL,
    http_port: int = HTTP_PORT,
) -> None:
    """在独立进程中运行 daemon（由 subprocess.Popen 调用）。"""
    _setup_logger()  # 幂等：重复调用不添加重复 handler
    daemon = TokenDaemon(scan_interval=scan_interval, http_port=http_port)
    if not daemon.start():
        log.warning("Daemon 启动失败（可能已在运行），退出")
        return
    daemon.run_forever()


def start_daemon(
    scan_interval: int = SCAN_INTERVAL,
    http_port: int = HTTP_PORT,
    foreground: bool = False,
) -> bool:
    """启动 Daemon 监控服务。

    Args:
        scan_interval: transcript 扫描间隔，单位秒（默认 30）
        http_port:    HTTP API 监听端口（默认 17890）
        foreground:   是否以前台模式运行（阻塞调用进程，输出日志到控制台）

    Returns:
        是否成功启动。已运行时返回 False。
    """
    if _is_pid_alive(_read_pid_static()):
        log.warning("Daemon 已在运行，请先执行 stop_daemon()")
        return False

    if foreground:
        # 前台模式：复用 _run_detached（在当前进程运行）
        _run_detached(scan_interval=scan_interval, http_port=http_port)
        return True
    else:
        # 后台模式：使用 subprocess 启动独立进程
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        # 确保 API token 存在（子进程也会检查，但提前生成避免竞态）
        if not _get_api_token():
            _generate_api_token()

        try:
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                [
                    sys.executable, "-c",
                    f"from claude_token_saver.daemon import _run_detached; "
                    f"_run_detached(scan_interval={scan_interval}, http_port={http_port})",
                ],
                **kwargs,
            )
            log.info("Daemon 子进程已启动 PID=%d", proc.pid)
            # 等待子进程写入 PID 文件
            for _ in range(20):
                pid = _read_pid_static()
                if pid and _is_pid_alive(pid):
                    return True
                time.sleep(0.5)
            return True
        except Exception as e:
            log.error("启动 daemon 子进程失败: %s", e)
            return False


def stop_daemon() -> bool:
    """停止正在运行的 Daemon。

    通过 PID 文件找到进程并发送 SIGTERM，等待优雅退出。

    Returns:
        是否成功停止。PID 文件不存在或进程已停止时返回 True。
    """
    pid = _read_pid_static()
    if pid is None:
        log.info("PID 文件不存在，daemon 未运行")
        return True

    if not _is_pid_alive(pid):
        log.info("进程 %d 已不存在，清理 PID 文件", pid)
        _cleanup_pid_file()
        return True

    log.info("正在停止 daemon (PID %d)...", pid)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except PermissionError:
        log.error("没有权限终止 PID %d", pid)
        return False
    except ProcessLookupError:
        _cleanup_pid_file()
        return True

    # 等待优雅退出（最多 20 秒）
    for _ in range(40):
        if not _is_pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        # 强制终止（Windows 无 SIGKILL，使用 taskkill）
        log.warning("进程 %d 未响应 SIGTERM，尝试强制终止", pid)
        _force_kill(pid)

    _cleanup_pid_file()
    log.info("Daemon (PID %d) 已停止", pid)
    return True


def get_daemon_status() -> dict[str, Any]:
    """获取 Daemon 运行状态。

    Returns:
        包含以下字段的字典：
        - running: 是否正在运行
        - pid: 进程 PID（如有）
        - pid_file: PID 文件路径
        - log_file: 日志文件路径
        - db_path: 数据库路径
        - transcripts_dir: transcript 目录路径
        - uptime_seconds: 运行时长（如可获取）
        - total_events: 累计解析事件数
        - total_sessions: 涉及会话数
        - total_alerts: 累计告警数
        - timestamp: 查询时间
    """
    pid = _read_pid_static()
    running = _is_pid_alive(pid)

    result: dict[str, Any] = {
        "running": running,
        "pid": pid,
        "pid_file": str(PID_FILE),
        "log_file": str(LOG_FILE),
        "db_path": str(DB_PATH),
        "transcripts_dir": ", ".join(str(d) for d in _get_agent_transcript_dirs()),
        "timestamp": _utcnow(),
    }

    # 统计数据库信息
    try:
        conn = _get_conn()
        result["total_events"] = conn.execute(
            "SELECT COUNT(*) FROM parsed_events"
        ).fetchone()[0]
        result["total_sessions"] = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM parsed_events"
        ).fetchone()[0]
        result["total_alerts"] = conn.execute(
            "SELECT COUNT(*) FROM alerts"
        ).fetchone()[0]
        conn.close()
    except Exception as e:
        log.debug("读取统计失败: %s", e)

    # 如果正在运行，尝试获取实时状态
    if running and pid:
        try:
            api_token = _get_api_token()
            if api_token:
                req_obj = urllib.request.Request(
                    f"http://127.0.0.1:{HTTP_PORT}/status",
                    headers={"Authorization": f"Bearer {api_token}"},
                )
                req = urllib.request.urlopen(req_obj, timeout=2)
                data = json.loads(req.read().decode("utf-8"))
                result["uptime_seconds"] = data.get("uptime_seconds")
                result["scanner_alive"] = data.get("scanner_alive")
                result["http_reachable"] = True
            else:
                result["http_reachable"] = False
        except Exception:
            result["http_reachable"] = False

    # 脱敏展示 API token（只读一次磁盘）
    api_token = _get_api_token()
    if api_token:
        result["api_token_prefix"] = api_token[:8]
        result["api_token_suffix"] = api_token[-4:]
        result["api_token_file"] = str(API_TOKEN_FILE)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# PID 文件辅助
# ═══════════════════════════════════════════════════════════════════════════════

def _read_pid_static() -> int | None:
    """静态方法：从 PID 文件读取 PID。"""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _cleanup_pid_file() -> None:
    """清理 PID 文件。"""
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
