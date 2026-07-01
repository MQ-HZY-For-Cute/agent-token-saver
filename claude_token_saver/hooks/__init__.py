"""
Agent Token Saver - Hook 系统
配置、安装、卸载 Claude Code Hooks。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any


def get_hook_command() -> str:
    """获取 hook handler 的完整命令。"""
    python_exe = sys.executable
    module_path = Path(__file__).parent / "handler.py"
    return f'"{python_exe}" "{module_path}"'


def generate_hook_config() -> dict:
    """生成 .claude/settings.json 的 hooks 配置。"""
    command = get_hook_command()

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                },
                {
                    "matcher": "Glob",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                },
                {
                    "matcher": "Grep",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "Read|Glob|Grep",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ],
        }
    }


def merge_json_config(base: dict, overlay: dict) -> dict:
    """安全合并两个 JSON 配置，保留原有字段，叠加新字段。

    递归合并字典；列表按元素去重合并（hooks 配置按 matcher 合并）。
    """
    result = _deep_copy(base)

    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_json_config(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            if _is_hooks_list(value) or _is_hooks_list(result[key]):
                result[key] = _merge_hooks_lists(result[key], value)
            else:
                merged = list(result[key])
                for item in value:
                    if item not in merged:
                        merged.append(item)
                result[key] = merged
        else:
            result[key] = _deep_copy(value)

    return result


def _deep_copy(obj: Any) -> Any:
    """深拷贝辅助，优先使用 copy.deepcopy，回退到 JSON 序列化。"""
    try:
        return copy.deepcopy(obj)
    except Exception:
        try:
            return json.loads(json.dumps(obj))
        except (TypeError, ValueError):
            return obj


def _is_hooks_list(lst: list) -> bool:
    """判断列表是否为 hooks 配置列表（元素包含 matcher + hooks 键）。"""
    return (
        isinstance(lst, list)
        and len(lst) > 0
        and all(isinstance(item, dict) and "matcher" in item and "hooks" in item for item in lst)
    )


def _merge_hooks_lists(base_list: list, overlay_list: list) -> list:
    """合并两个 hooks matcher 列表，按 matcher 去重，hooks 列表追加。"""
    existing = {m.get("matcher", ""): m for m in base_list if isinstance(m, dict)}

    for new_entry in overlay_list:
        if not isinstance(new_entry, dict):
            continue
        matcher_key = new_entry.get("matcher", "")
        if matcher_key in existing:
            existing_hooks = existing[matcher_key].get("hooks", [])
            new_hooks = new_entry.get("hooks", [])
            if isinstance(existing_hooks, list) and isinstance(new_hooks, list):
                merged_hooks = list(existing_hooks)
                for hook in new_hooks:
                    if hook not in merged_hooks:
                        merged_hooks.append(hook)
                merged_entry = dict(existing[matcher_key])
                merged_entry["hooks"] = merged_hooks
                existing[matcher_key] = merged_entry
        else:
            existing[matcher_key] = _deep_copy(new_entry)

    return list(existing.values())


def install_hooks(dry_run: bool = False) -> str:
    """将 hook 配置合并写入 .claude/settings.local.json。"""
    settings_local_path = Path.home() / ".claude" / "settings.local.json"
    settings_local_path.parent.mkdir(parents=True, exist_ok=True)

    hook_config = generate_hook_config()

    if dry_run:
        return str(settings_local_path)

    if settings_local_path.exists():
        try:
            with open(settings_local_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except (json.JSONDecodeError, IOError):
            existing = {}
    else:
        existing = {}

    merged = merge_json_config(existing, hook_config)

    with open(settings_local_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    return str(settings_local_path)


def uninstall_hooks() -> str:
    """移除 hook 配置。"""
    settings_local_path = Path.home() / ".claude" / "settings.local.json"

    if not settings_local_path.exists():
        return str(settings_local_path)

    try:
        with open(settings_local_path, "r", encoding="utf-8") as f:
            existing = json.load(f) or {}
    except (json.JSONDecodeError, IOError):
        return str(settings_local_path)

    if "hooks" in existing:
        del existing["hooks"]

    with open(settings_local_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return str(settings_local_path)
