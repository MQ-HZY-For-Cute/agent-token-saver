"""
Agent Token Saver - 自适应预算分配器

核心思路：不靠用户手动指定压缩级别，而是根据剩余 token 预算
自动为每个文件分配最优的 detail_level。

策略：
  - 按文件 token 大小从大到小排序
  - 超大文件（>预算 50%）→ skeleton（若超总预算则 block）
  - 大文件（>预算 15%）→ skeleton
  - 中文件（>预算 5%）→ stripped
  - 小文件 → full
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_token_saver.utils import count_tokens


def auto_detail_level(
    file_tokens: list[tuple[str, int]],
    total_budget: int,
) -> dict[str, str]:
    """为每个文件自动分配最优 detail_level。

    Args:
        file_tokens: [(file_path, token_count), ...]
        total_budget: 总 token 预算

    Returns:
        {file_path: detail_level, ...}
    """
    if total_budget <= 0:
        return {fp: "block" for fp, _ in file_tokens}

    # 按 token 从大到小排序（大文件先分配，确保重要文件不被挤掉）
    sorted_files = sorted(file_tokens, key=lambda x: x[1], reverse=True)

    levels: dict[str, str] = {}
    remaining = total_budget

    for fp, tokens in sorted_files:
        if tokens > total_budget * 0.5:
            # 单个文件超过总预算一半 → skeleton 或 block
            if tokens > total_budget:
                levels[fp] = "block"
                continue
            levels[fp] = "skeleton"
        elif tokens > total_budget * 0.15:
            levels[fp] = "skeleton"
        elif tokens > total_budget * 0.05:
            levels[fp] = "stripped"
        else:
            levels[fp] = "full"

    return levels


def estimate_auto_cost(
    file_tokens: list[tuple[str, int]],
    total_budget: int,
) -> dict:
    """估算自适应分配后的总 token 消耗。

    Returns:
        {
            "total_before": int,
            "estimated_after": int,
            "levels_assigned": {fp: level},
            "skipped": [fp, ...],
            "savings_pct": float,
        }
    """
    from claude_token_saver.compressor import extract_skeleton, extract_symbol_index
    from claude_token_saver.prep import strip_comments, _clear_caches
    from claude_token_saver.utils import count_tokens

    levels = auto_detail_level(file_tokens, total_budget)
    total_before = sum(t for _, t in file_tokens)
    total_after = 0
    skipped = []

    # 压缩率估算（基于经验值，避免逐文件实际处理）
    compression_ratios = {
        "skeleton": 0.15,
        "stripped": 0.55,
        "full": 1.0,
        "block": 0.0,
    }

    for fp, tokens in file_tokens:
        level = levels.get(fp, "full")
        if level == "block":
            skipped.append(fp)
            continue
        ratio = compression_ratios.get(level, 1.0)
        total_after += int(tokens * ratio)

    savings_pct = ((total_before - total_after) / total_before * 100) if total_before else 0

    return {
        "total_before": total_before,
        "estimated_after": total_after,
        "levels_assigned": levels,
        "skipped": skipped,
        "savings_pct": round(savings_pct, 1),
    }
