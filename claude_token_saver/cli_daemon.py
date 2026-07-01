"""
daemon 子命令 - Token Saver Daemon 后台监控服务
"""
from __future__ import annotations

import os
import subprocess
import sys

import click

from claude_token_saver.daemon import (
    TokenDaemon,
    PID_FILE,
    LOG_FILE,
    get_daemon_status,
)


@click.group()
def daemon() -> None:
    """管理 Token Saver 后台监控服务。"""
    pass


@daemon.command("start")
@click.option("--interval", "-i", type=int, default=30, help="transcript 扫描间隔（秒），默认 30")
@click.option("--port", "-p", type=int, default=17890, help="HTTP API 端口，默认 17890")
@click.option("--foreground", "-f", is_flag=True, help="前台模式运行（输出日志到控制台）")
def daemon_start(interval: int, port: int, foreground: bool) -> None:
    """启动 Daemon 监控服务。"""
    from claude_token_saver.daemon import start_daemon

    # 检查是否已在运行（优先 HTTP API，回退 PID 文件）
    from claude_token_saver.daemon import get_daemon_status
    status_check = get_daemon_status()
    if status_check.get("running"):
        click.echo(click.style(f"⚠️  Daemon 已在运行 (PID: {status_check.get('pid', 'unknown')})", fg="yellow"))
        click.echo("使用 'cts daemon status' 查看状态，'cts daemon stop' 停止")
        return

    if foreground:
        click.echo(click.style("🔧 启动 Daemon（前台模式）...", fg="cyan"))
        click.echo(f"   扫描间隔: {interval}s  |  HTTP API: http://127.0.0.1:{port}")
        click.echo("   按 Ctrl+C 停止\n")
    else:
        click.echo(click.style("🔧 启动 Daemon（后台模式）...", fg="cyan"))
        click.echo(f"   扫描间隔: {interval}s  |  HTTP API: http://127.0.0.1:{port}")
        click.echo(f"   日志文件: {LOG_FILE}")

    ok = start_daemon(scan_interval=interval, http_port=port, foreground=foreground)
    if ok:
        if foreground:
            click.echo(click.style("🛑 Daemon 已停止", fg="yellow"))
        else:
            click.echo(click.style("✅ Daemon 启动成功", fg="green"))
            status = get_daemon_status()
            click.echo(f"   PID: {status.get('pid', 'unknown')}")
    else:
        click.echo("❌ Daemon 启动失败", err=True)
        raise SystemExit(1)


@daemon.command("stop")
@click.option("--force", is_flag=True, help="强制终止进程")
def daemon_stop(force: bool) -> None:
    """停止 Daemon 监控服务。"""
    from claude_token_saver.daemon import stop_daemon, get_daemon_status

    # 优先通过 HTTP API 检测 daemon 是否运行
    http_status = get_daemon_status()
    if not http_status.get("running"):
        click.echo(click.style("📭 Daemon 未运行", fg="yellow"))
        # 清理 PID 文件
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return

    pid = http_status.get("pid")

    if force:
        click.echo(f"🔨 强制终止 Daemon (PID: {pid})...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            else:
                os.kill(pid, 9)
        except (OSError, subprocess.TimeoutExpired) as e:
            click.echo(f"⚠️  强制终止失败: {e}", err=True)
        click.echo(click.style("✅ Daemon 已强制停止", fg="green"))
        return

    click.echo(f"🛑 正在停止 Daemon (PID: {pid})...")
    ok = stop_daemon()
    if ok:
        click.echo(click.style("✅ Daemon 已停止", fg="green"))
    else:
        click.echo("❌ 停止失败", err=True)
        raise SystemExit(1)


@daemon.command("status")
@click.option("--json", "json_output", is_flag=True, help="JSON 格式输出")
def daemon_status(json_output: bool) -> None:
    """查看 Daemon 运行状态。"""
    status = get_daemon_status()

    if json_output:
        click.echo(click.style(
            json.dumps(status, indent=2, ensure_ascii=False, default=str),
            fg="cyan"
        ))
        return

    # 文本输出
    if status.get("running"):
        click.echo(click.style("🟢 Daemon 运行中", bold=True, fg="green"))
    else:
        click.echo(click.style("🔴 Daemon 已停止", bold=True, fg="red"))

    click.echo(f"   PID:           {status.get('pid') or 'N/A'}")
    click.echo(f"   PID 文件:      {status.get('pid_file', 'N/A')}")
    click.echo(f"   日志文件:      {status.get('log_file', 'N/A')}")
    click.echo(f"   数据库:        {status.get('db_path', 'N/A')}")
    click.echo(f"   Transcript 目录: {status.get('transcripts_dir', 'N/A')}")

    if status.get("uptime_seconds") is not None:
        hours = status["uptime_seconds"] // 3600
        minutes = (status["uptime_seconds"] % 3600) // 60
        click.echo(f"   运行时长:      {hours}h {minutes}m")

    if status.get("http_reachable"):
        click.echo(click.style("   HTTP API:       ✅ 可达", fg="green"))
    else:
        click.echo("   HTTP API:       ❌ 不可达")

    if status.get("api_token_prefix"):
        click.echo(f"   API Token:      {status['api_token_prefix']}...{status['api_token_suffix']}")
        click.echo(f"   Token 文件:     {status.get('api_token_file', 'N/A')}")

    click.echo(f"\n   📊 累计解析事件:  {status.get('total_events', 0):,}")
    click.echo(f"   📁 涉及会话数:    {status.get('total_sessions', 0)}")
    click.echo(f"   ⚠️  累计告警数:    {status.get('total_alerts', 0)}")

    click.echo(f"\n   查询时间: {status.get('timestamp', 'N/A')}")


@daemon.command("logs")
@click.option("--lines", "-n", type=int, default=50, help="显示最近 N 行，默认 50")
@click.option("--follow", "-f", is_flag=True, help="实时跟踪日志（类似 tail -f）")
@click.option("--level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]), help="过滤日志级别")
def daemon_logs(lines: int, follow: bool, level: str | None) -> None:
    """查看 Daemon 日志。"""
    if not LOG_FILE.exists():
        click.echo(click.style("📭 日志文件不存在，Daemon 可能尚未启动过", fg="yellow"))
        click.echo(f"   路径: {LOG_FILE}")
        return

    if follow:
        click.echo(click.style(f"👀 实时跟踪日志: {LOG_FILE}（Ctrl+C 停止）\n", fg="cyan"))
        import time
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                # 跳到文件末尾
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        if level and not line.startswith(f"{level}"):
                            continue
                        click.echo(line.rstrip())
                    else:
                        time.sleep(0.5)
        except KeyboardInterrupt:
            click.echo("\n👋 停止跟踪")
        return

    # 静态读取最后 N 行
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        # 过滤日志级别
        if level:
            filtered = [l for l in all_lines if l.startswith(level)]
        else:
            filtered = all_lines

        # 取最后 N 行
        tail = filtered[-lines:] if lines > 0 else filtered

        if not tail:
            click.echo("📭 日志为空")
            return

        click.echo(click.style(f"📜 最近 {len(tail)} 行日志（共 {len(all_lines)} 行）:\n", fg="cyan"))
        for line in tail:
            click.echo(line.rstrip())
    except (OSError, IOError) as e:
        click.echo(f"❌ 读取日志失败: {e}", err=True)
        raise SystemExit(1)
