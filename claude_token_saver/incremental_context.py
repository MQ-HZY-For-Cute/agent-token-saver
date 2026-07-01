"""
Agent Token Saver - 增量上下文系统

核心思路：在多轮对话中，同一文件的内容经常被重复发送。
通过跟踪已发送的文件，后续轮次只发送变更的部分。

策略：
  - NEW/MODIFIED：发送完整内容
  - UNCHANGED：仅引用路径（"已发送，见上文"）
  - DELETED：发送移除通知

适用场景：
  - 多轮对话中重复读取同一组文件
  - Watch 模式下持续监控文件变化
  - Session 间共享上下文
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_token_saver.utils import count_tokens


@dataclass
class SentFileRecord:
    """已发送文件的记录。"""
    path: str
    content_hash: str
    token_count: int
    sent_at_turn: int = 0
    timestamp: str = ""


@dataclass
class FileChange:
    """文件变更记录。"""
    path: str
    change_type: str  # "new" | "modified" | "unchanged" | "deleted"
    content_hash: str = ""
    tokens: int = 0


class ContentHashCache:
    """内容哈希缓存：跟踪已发送文件。"""

    def __init__(self):
        self._cache: dict[str, SentFileRecord] = {}

    def record_sent(self, path: str, content: str, turn: int = 0) -> None:
        """记录一个文件已被发送。"""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        self._cache[path] = SentFileRecord(
            path=path,
            content_hash=content_hash,
            token_count=count_tokens(content),
            sent_at_turn=turn,
        )

    def is_changed(self, path: str, content: str) -> bool:
        """检查文件内容是否与上次发送时不同。"""
        if path not in self._cache:
            return True
        new_hash = hashlib.sha256(content.encode()).hexdigest()
        return self._cache[path].content_hash != new_hash

    def get_record(self, path: str) -> SentFileRecord | None:
        """获取文件的发送记录。"""
        return self._cache.get(path)

    def remove(self, path: str) -> None:
        """移除文件的发送记录（文件被删除时调用）。"""
        self._cache.pop(path, None)

    def clear(self) -> None:
        """清空缓存。"""
        self._cache.clear()

    def snapshot(self) -> dict[str, SentFileRecord]:
        """返回当前缓存快照。"""
        return dict(self._cache)


def detect_file_changes(
    current_files: list[str | Path],
    sent_cache: ContentHashCache,
    read_content_fn=None,
) -> list[FileChange]:
    """检测文件变更情况。

    Args:
        current_files: 当前文件路径列表
        sent_cache: 已发送文件缓存
        read_content_fn: 可选的函数(path) -> content，用于检测内容变更。
                         如果为 None，所有不存在的文件都视为 new。

    Returns:
        FileChange 列表
    """
    changes: list[FileChange] = []
    current_paths = {str(p) for p in current_files}

    # 检查当前文件
    for fp in current_files:
        fp_str = str(fp)
        record = sent_cache.get_record(fp_str)

        if record is None:
            # 新文件
            content_hash = ""
            tokens = 0
            if read_content_fn:
                try:
                    content = read_content_fn(fp)
                    content_hash = hashlib.sha256(content.encode()).hexdigest()
                    tokens = count_tokens(content)
                except Exception:
                    pass
            changes.append(FileChange(path=fp_str, change_type="new",
                                      content_hash=content_hash, tokens=tokens))
        else:
            # 已发送过，检查是否变更
            content_changed = True
            content_hash = record.content_hash
            if read_content_fn:
                try:
                    content = read_content_fn(fp)
                    content_hash = hashlib.sha256(content.encode()).hexdigest()
                    content_changed = content_hash != record.content_hash
                except Exception:
                    content_changed = True

            if content_changed:
                changes.append(FileChange(path=fp_str, change_type="modified",
                                          content_hash=content_hash, tokens=record.token_count))
            else:
                changes.append(FileChange(path=fp_str, change_type="unchanged",
                                          content_hash=content_hash, tokens=0))

    # 检查已删除的文件
    for path in list(sent_cache._cache.keys()):
        if path not in current_paths:
            changes.append(FileChange(path=path, change_type="deleted"))
            sent_cache.remove(path)

    return changes


def format_incremental_context(
    changes: list[FileChange],
    sent_cache: ContentHashCache,
    get_content_fn=None,
    format: str = "markdown",
) -> str:
    """将文件变更格式化为增量上下文文本。

    Args:
        changes: FileChange 列表
        sent_cache: 已发送文件缓存
        get_content_fn: 可选函数(path) -> content，获取文件完整内容
        format: 输出格式 ("markdown" | "plain")

    Returns:
        格式化的增量上下文文本
    """
    new_files = [c for c in changes if c.change_type == "new"]
    modified_files = [c for c in changes if c.change_type == "modified"]
    unchanged_files = [c for c in changes if c.change_type == "unchanged"]
    deleted_files = [c for c in changes if c.change_type == "deleted"]

    parts = []
    parts.append(f"# 增量上下文（{len(changes)} 个文件变更）\n")

    # 新增文件
    if new_files:
        parts.append(f"## 新增文件（{len(new_files)} 个）\n")
        for cf in new_files:
            parts.append(f"### `{cf.path}`\n")
            if get_content_fn:
                try:
                    content = get_content_fn(cf.path)
                    parts.append(f"```\n{content}\n```\n")
                except Exception:
                    parts.append(f"(无法读取: {cf.path})\n")

    # 修改的文件
    if modified_files:
        parts.append(f"## 修改的文件（{len(modified_files)} 个）\n")
        for cf in modified_files:
            parts.append(f"### `{cf.path}`\n")
            if get_content_fn:
                try:
                    content = get_content_fn(cf.path)
                    parts.append(f"```\n{content}\n```\n")
                except Exception:
                    parts.append(f"(无法读取: {cf.path})\n")

    # 未变更的文件引用
    if unchanged_files:
        parts.append(f"## 未变更的文件（{len(unchanged_files)} 个，见上文）\n")
        for cf in unchanged_files:
            parts.append(f"- `{cf.path}`（已发送，内容未变更）")

    # 已删除
    if deleted_files:
        parts.append(f"\n## 已删除的文件（{len(deleted_files)} 个）\n")
        for cf in deleted_files:
            parts.append(f"- `{cf.path}`（已移除）")

    return "\n".join(parts)


class IncrementalContextManager:
    """增量上下文管理器：在会话级别跟踪已发送文件。

    使用方式：
        mgr = IncrementalContextManager(session_id="s1")
        # 第一轮：发送所有文件
        text = mgr.format_full_context(file_list, get_content_fn)

        # 后续轮次：只发送变更
        changes = mgr.detect_changes(current_files, get_content_fn)
        text = mgr.format_incremental(changes, get_content_fn)
    """

    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self.cache = ContentHashCache()
        self._turn_counter = 0

    def new_turn(self) -> None:
        """开始新的一轮。"""
        self._turn_counter += 1

    def mark_sent(self, path: str, content: str) -> None:
        """标记文件已在本轮发送。"""
        self.cache.record_sent(path, content, turn=self._turn_counter)

    def detect_changes(
        self,
        current_files: list[str | Path],
        read_content_fn=None,
    ) -> list[FileChange]:
        """检测当前文件列表与已发送文件的差异。"""
        self.new_turn()
        changes = detect_file_changes(current_files, self.cache, read_content_fn)

        # 将 new/modified 的文件记录到缓存
        for cf in changes:
            if cf.change_type in ("new", "modified") and read_content_fn:
                try:
                    content = read_content_fn(cf.path)
                    self.cache.record_sent(cf.path, content, turn=self._turn_counter)
                except Exception:
                    pass
            elif cf.change_type == "deleted":
                self.cache.remove(cf.path)

        return changes

    def format_full_context(
        self,
        file_paths: list[str | Path],
        get_content_fn,
        format: str = "markdown",
    ) -> str:
        """格式化完整上下文（首次发送时使用）。"""
        self.new_turn()
        changes: list[FileChange] = []
        for fp in file_paths:
            fp_str = str(fp)
            try:
                content = get_content_fn(fp) if get_content_fn else ""
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                tokens = count_tokens(content)
                changes.append(FileChange(path=fp_str, change_type="new",
                                          content_hash=content_hash, tokens=tokens))
                self.cache.record_sent(fp_str, content, turn=self._turn_counter)
            except Exception:
                changes.append(FileChange(path=fp_str, change_type="new"))

        return format_incremental_context(changes, self.cache, get_content_fn, format)

    def format_incremental(
        self,
        changes: list[FileChange],
        get_content_fn,
        format: str = "markdown",
    ) -> str:
        """格式化增量上下文。"""
        self.new_turn()

        # 更新缓存
        for cf in changes:
            if cf.change_type in ("new", "modified") and get_content_fn:
                try:
                    content = get_content_fn(cf.path)
                    self.cache.record_sent(cf.path, content, turn=self._turn_counter)
                except Exception:
                    pass
            elif cf.change_type == "deleted":
                self.cache.remove(cf.path)

        return format_incremental_context(changes, self.cache, get_content_fn, format)

    def get_stats(self) -> dict:
        """获取统计信息。"""
        total_sent = len(self.cache._cache)
        current_turn = self._turn_counter
        return {
            "session_id": self.session_id,
            "total_files_sent": total_sent,
            "current_turn": current_turn,
        }
