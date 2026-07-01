"""
Agent Token Saver - 主入口 CLI
"""
from __future__ import annotations

import sys
import io

# 强制 UTF-8 输出（Windows GBK 终端兼容）
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import click

from claude_token_saver.cli_prep import prep
from claude_token_saver.cli_sessions import sessions
from claude_token_saver.cli_stats import stats
from claude_token_saver.cli_tui import tui
from claude_token_saver.cli_transcript import transcript
from claude_token_saver.cli_hooks import hooks
from claude_token_saver.cli_daemon import daemon
from claude_token_saver.cli_agents import agents
from claude_token_saver.config import load_config, save_config
from claude_token_saver.agents import _AGENT_CHOICES, resolve_adapter


def setup_auto() -> None:
    """一键安装（独立入口: ats-setup 命令）。"""
    from claude_token_saver.daemon import start_daemon

    click.echo(click.style("🔍 检测环境...", bold=True))
    adapter = resolve_adapter(None)
    click.echo(click.style(f"  → {adapter.agent_name} ({adapter.agent_id.value})", fg="cyan"))

    click.echo("")
    click.echo(click.style("🔧 安装 hooks...", bold=True))
    try:
        path = adapter.install_config()
        click.echo(click.style(f"  ✅ {path}", fg="green"))
    except Exception as e:
        click.echo(f"  ❌ 安装失败: {e}", err=True)

    click.echo("")
    click.echo(click.style("📡 启动 daemon...", bold=True))
    try:
        if start_daemon():
            click.echo(click.style("  ✅ 已启动", fg="green"))
        else:
            click.echo("  ℹ️  已在运行中")
    except Exception as e:
        click.echo(f"  ⚠️  {e}", fg="yellow")

    click.echo("")
    click.echo(click.style("🎉 完成!", bold=True, fg="green"))


@click.group()
@click.version_option("0.2.0", prog_name="agent-token-saver")
@click.pass_context
def main(ctx: click.Context) -> None:
    """AI Coding Agent Token Saver — 一键减少 token 消耗。

    \b
    新用户（未安装）: 直接运行 cts-setup 或 cts setup
    """
    ctx.ensure_object(dict)


main.add_command(prep)
main.add_command(sessions)
main.add_command(stats)
main.add_command(tui)
main.add_command(transcript)
main.add_command(hooks)
main.add_command(daemon)
main.add_command(agents)


@main.command("doctor")
def doctor_cmd() -> None:
    """运行健康检查（同 `agents check`，更易记忆）。"""
    from claude_token_saver.cli_agents import agents_check
    ctx = click.get_current_context()
    ctx.forward(agents_check)


@main.command("setup")
@click.option("--agent", "-a",
    type=click.Choice(_AGENT_CHOICES, case_sensitive=False),
    default=None, help="指定 Agent（默认: 自动检测）")
@click.option("--dry-run", is_flag=True, help="只显示操作，不执行")
@click.option("--skip-daemon", is_flag=True, help="不启动 daemon")
@click.option("--skip-hooks", is_flag=True, help="不安装 hooks")
def setup_cmd(agent: str | None, dry_run: bool, skip_daemon: bool, skip_hooks: bool) -> None:
    """一键完成初始化：检测 Agent → 安装 hooks → 启动 daemon。

    \b
    示例:
      cts setup                       # 自动检测 + 交互式安装
      cts setup --agent claude        # 指定 Agent
      cts setup --dry-run             # 预览操作
      cts setup --skip-daemon         # 只安装 hooks
    """
    from claude_token_saver.daemon import start_daemon

    # 1. 检测
    click.echo(click.style("🔍 检测环境...", bold=True))
    adapter = resolve_adapter(agent)
    click.echo(click.style(f"  → {adapter.agent_name} ({adapter.agent_id.value})", fg="cyan"))

    # 2. 安装 hooks
    if not skip_hooks:
        click.echo("")
        click.echo(click.style("🔧 安装 hooks...", bold=True))
        if dry_run:
            click.echo(f"  目标: {adapter.config.settings_path}")
            click.echo("  (dry-run)")
        else:
            try:
                path = adapter.install_config()
                click.echo(click.style(f"  ✅ {path}", fg="green"))
            except Exception as e:
                click.echo(f"  ❌ 安装失败: {e}", err=True)

    # 3. 启动 daemon
    if not skip_daemon and not dry_run:
        click.echo("")
        click.echo(click.style("📡 启动 daemon...", bold=True))
        try:
            if start_daemon():
                click.echo(click.style("  ✅ 已启动", fg="green"))
            else:
                click.echo("  ℹ️  已在运行中")
        except Exception as e:
            click.echo(f"  ⚠️  {e}", fg="yellow")

    # 4. 总结
    click.echo("")
    click.echo(click.style("🎉 完成!", bold=True, fg="green"))
    click.echo("")
    click.echo("下一步:")
    click.echo(f"  cts hooks test -t Read                    # 测试 hooks")
    click.echo(f"  cts agents test --agent {adapter.agent_id.value} -t Read  # 多 Agent 测试")
    click.echo("  cts stats report                           # 查看 token 报告")
    click.echo("  cts daemon status                          # daemon 状态")


@main.command("init")
def init_cmd() -> None:
    """初始化项目（安装依赖、创建配置）。"""
    click.echo("🔧 初始化 agent-token-saver...")
    config = load_config()
    click.echo(f"✅ 配置文件已创建: {config.get('_path', '~/.agent-token-saver/config.yaml')}")
    click.echo("✅ 数据库已初始化")
    click.echo("")
    click.echo("安装依赖（需要 Python 3.10+）：")
    click.echo("  pip install -e .")
    click.echo("")
    click.echo("快速开始：")
    click.echo("  cts setup                        # 一键完成全部配置")
    click.echo("  cts agents list                  # 查看支持的 Agent")
    click.echo("  cts agents detect                # 检测当前 Agent")
    click.echo("  cts agents install               # 安装 hooks")
    click.echo("  cts prep files/ --dry-run        # 预览处理效果")
    click.echo("  cts stats report                  # 查看 token 报告")


@main.command("config")
@click.option("--show", is_flag=True, help="显示当前配置")
@click.option("--set", "set_key", help="设置配置项 (格式: key=value)")
@click.option("--reset", is_flag=True, help="重置为默认配置")
def config_cmd(show: bool, set_key: str | None, reset: bool) -> None:
    """查看或修改配置。"""
    from claude_token_saver.cli_helpers import handle_config

    handle_config(show, set_key, reset)


if __name__ == "__main__":
    main()
