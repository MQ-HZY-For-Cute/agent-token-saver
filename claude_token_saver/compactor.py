"""
Agent Token Saver - 对话上下文压缩器

核心思路：旧轮次完全可以用摘要替代，释放大量 token。

策略：
  - 最近 N 轮保留完整内容
  - 中间的轮次压缩为摘要（保留关键决策和结果）
  - 更早的轮次丢弃（可存入 DB 按需恢复）

压缩流程：
  1. 检测对话 token 是否超过阈值
  2. 触发 compact：将旧轮次压缩为摘要
  3. 摘要存入 DB（compact_log）
  4. 返回精简的对话历史
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_token_saver.utils import count_tokens
from claude_token_saver.sessions import SessionManager, DB_PATH


@dataclass
class TurnSummary:
    """单轮对话的压缩摘要。"""
    turn_index: int
    turn_type: str  # "user" | "assistant"
    summary: str
    tokens_before: int = 0
    tokens_after: int = 0
    tool_calls: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class CompactedContext:
    """压缩后的对话上下文。"""
    session_id: str
    original_turns: int
    compacted_turns: int
    summaries: list[TurnSummary]
    recent_turns: list[dict]  # 最近几轮的完整内容
    total_tokens_before: int = 0
    total_tokens_after: int = 0


class ConversationCompactor:
    """对话上下文压缩器。

    使用方式：
        compactor = ConversationCompactor()
        result = compactor.compact_if_needed(session_id, turns, threshold)
        if result:
            # 返回紧凑版本，替换原始对话历史
            compact_context = result
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.session_mgr = SessionManager(db_path=self.db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接。"""
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        """初始化对话摘要表。"""
        with contextlib.closing(self._get_conn()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turn_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    turn_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tokens_before INTEGER DEFAULT 0,
                    tokens_after INTEGER DEFAULT 0,
                    tool_calls TEXT DEFAULT '[]',
                    key_decisions TEXT DEFAULT '[]',
                    timestamp TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS compacted_contexts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    original_turns INTEGER DEFAULT 0,
                    compacted_turns INTEGER DEFAULT 0,
                    total_tokens_before INTEGER DEFAULT 0,
                    total_tokens_after INTEGER DEFAULT 0,
                    context_data TEXT DEFAULT '{}',
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summaries_session ON turn_summaries(session_id)"
            )
            conn.commit()

    def should_compact(self, session_id: str, total_tokens: int, threshold: int = 100_000) -> bool:
        """判断是否需要触发 compact。"""
        session = self.session_mgr.get_session(session_id)
        if session and session.compacted:
            # 已经 compact 过，使用更高阈值
            threshold = int(threshold * 1.5)
        return total_tokens > threshold

    def compact_if_needed(
        self,
        session_id: str,
        turns: list[dict],
        threshold: int = 100_000,
        keep_recent: int = 5,
    ) -> CompactedContext | None:
        """如果超过阈值则执行 compact。"""
        total_tokens = sum(t.get("tokens", count_tokens(t.get("content", ""))) for t in turns)

        if not self.should_compact(session_id, total_tokens, threshold):
            return None

        return self.compact(session_id, turns, keep_recent)

    def compact(
        self,
        session_id: str,
        turns: list[dict],
        keep_recent: int = 5,
    ) -> CompactedContext:
        """执行 compact：将旧轮次压缩为摘要，保留最近 N 轮完整内容。"""
        if not turns:
            return CompactedContext(
                session_id=session_id,
                original_turns=0,
                compacted_turns=0,
                summaries=[],
                recent_turns=[],
            )

        total_before = sum(
            t.get("tokens", count_tokens(t.get("content", ""))) for t in turns
        )

        # 分离：最近 N 轮保留完整，旧轮次压缩为摘要
        split_at = max(0, len(turns) - keep_recent) if keep_recent < len(turns) else len(turns)
        recent = turns[split_at:]
        old_turns = turns[:split_at]

        summaries: list[TurnSummary] = []
        for turn in old_turns:
            summary_text = self._summarize_turn(turn)
            tokens = turn.get("tokens", count_tokens(turn.get("content", "")))
            summary_tokens = count_tokens(summary_text)

            tool_calls = []
            if turn.get("tool_uses"):
                tool_calls = [t.get("name", "") for t in turn.get("tool_uses", [])]

            key_decisions = self._extract_key_decisions(turn.get("content", ""))

            summaries.append(TurnSummary(
                turn_index=turn.get("turn_index", 0),
                turn_type=turn.get("type", "user"),
                summary=summary_text,
                tokens_before=tokens,
                tokens_after=summary_tokens,
                tool_calls=tool_calls,
                key_decisions=key_decisions,
                timestamp=turn.get("timestamp", ""),
            ))

        # 保存摘要到 DB
        self._save_summaries(session_id, summaries)

        # 计算压缩后 token
        total_after = sum(s.tokens_after for s in summaries)
        total_after += sum(
            t.get("tokens", count_tokens(t.get("content", "")))
            for t in recent
        )

        # 记录 compact 操作
        self.session_mgr.log_compact(session_id, total_before, total_after)
        self.session_mgr.update_session(session_id, compacted=True)

        # 保存 compacted context
        self._save_compacted_context(
            session_id=session_id,
            original=len(turns),
            compacted=len(summaries) + len(recent),
            tokens_before=total_before,
            tokens_after=total_after,
            data={"summaries": [s.summary for s in summaries],
                  "recent_count": len(recent)},
        )

        return CompactedContext(
            session_id=session_id,
            original_turns=len(turns),
            compacted_turns=len(summaries) + len(recent),
            summaries=summaries,
            recent_turns=recent,
            total_tokens_before=total_before,
            total_tokens_after=total_after,
        )

    def get_compacted_context(self, session_id: str) -> CompactedContext | None:
        """从 DB 恢复压缩后的上下文。"""
        with contextlib.closing(self._get_conn()) as conn:
            row = conn.execute(
                "SELECT context_data FROM compacted_contexts "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1",
                (session_id,)
            ).fetchone()

        if not row:
            return None

        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

        summaries = self._load_summaries(session_id)

        return CompactedContext(
            session_id=session_id,
            original_turns=data.get("original_turns", 0),
            compacted_turns=data.get("compacted_turns", 0),
            summaries=summaries,
            recent_turns=[],  # 完整轮次不持久化，需从 transcript 恢复
            total_tokens_before=data.get("total_tokens_before", 0),
            total_tokens_after=data.get("total_tokens_after", 0),
        )

    def format_for_prompt(self, context: CompactedContext) -> str:
        """将压缩上下文格式化为可注入 prompt 的文本（紧凑格式）。"""
        parts: list[str] = []
        saved = context.total_tokens_before - context.total_tokens_after
        pct = (saved / context.total_tokens_before * 100) if context.total_tokens_before else 0
        parts.append(f"[摘要 {context.original_turns}→{context.compacted_turns}轮 节省{saved}tok {pct:.0f}%]")

        for s in context.summaries:
            line = f"T{s.turn_index}{'U' if s.turn_type == 'user' else 'A'}:{s.summary}"
            if s.tool_calls:
                line += f"  tools:{','.join(s.tool_calls[:2])}"
            parts.append(line)

        if context.recent_turns:
            parts.append(f"\n[最近{len(context.recent_turns)}轮]")
            for turn in context.recent_turns:
                content = turn.get("content", "")[:300]
                parts.append(f"T{turn.get('turn_index', '?')}F:{content}")

        return "\n".join(parts)

    def _summarize_turn(self, turn: dict) -> str:
        """将单轮对话压缩为摘要。"""
        content = turn.get("content", "")
        turn_type = turn.get("type", "user")

        # 助手回复有工具调用：生成结构化摘要
        tool_uses = turn.get("tool_uses", [])
        if turn_type != "user" and tool_uses:
            tools = [t.get("name", "") for t in tool_uses]
            result_parts = []
            for t in tool_uses:
                rc = t.get("result_content", "")[:80]
                if rc:
                    result_parts.append(f"{t.get('name', '')}: {rc}")
            summary = f"调用了 {', '.join(tools)}"
            if result_parts:
                summary += f" | {'; '.join(result_parts[:2])}"
            return summary

        # 纯文本：在句子边界处智能截断
        if not content:
            return "(空)"

        max_chars = 80 if turn_type == "user" else 120
        if len(content) <= max_chars:
            return content

        # 尝试在句号处截断，保持语义完整
        truncated = content[:max_chars + 1]  # +1 以便检测句号在 max_chars 处
        candidates = []
        for marker in ("。", ". ", ".\n", " ", "\n"):
            pos = truncated.rfind(marker)
            if pos > max_chars * 0.5:
                candidates.append(pos)

        if candidates:
            return content[:max(candidates) + 1].rstrip() + "..."
        return content[:max_chars].rstrip() + "..."

    def _extract_key_decisions(self, content: str) -> list[str]:
        """从内容中提取关键决策句。"""
        decisions = []
        keywords = ["决定", "选择", "改为", "使用", "放弃", "确认", "结论"]
        for line in content.split("\n"):
            stripped = line.strip()
            if any(kw in stripped for kw in keywords) and len(stripped) < 100:
                decisions.append(stripped)
        return decisions[:3]  # 最多 3 个

    def _save_summaries(self, session_id: str, summaries: list[TurnSummary]) -> None:
        """保存摘要到 DB。"""
        with contextlib.closing(self._get_conn()) as conn:
            for s in summaries:
                conn.execute(
                    "INSERT OR REPLACE INTO turn_summaries "
                    "(session_id, turn_index, turn_type, summary, tokens_before, "
                    "tokens_after, tool_calls, key_decisions, timestamp, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id, s.turn_index, s.turn_type, s.summary,
                        s.tokens_before, s.tokens_after,
                        json.dumps(s.tool_calls, ensure_ascii=False),
                        json.dumps(s.key_decisions, ensure_ascii=False),
                        s.timestamp, datetime.now().isoformat(),
                    ),
                )
            conn.commit()

    def _load_summaries(self, session_id: str) -> list[TurnSummary]:
        """从 DB 加载摘要。"""
        with contextlib.closing(self._get_conn()) as conn:
            rows = conn.execute(
                "SELECT turn_index, turn_type, summary, tokens_before, tokens_after, "
                "tool_calls, key_decisions, timestamp FROM turn_summaries "
                "WHERE session_id = ? ORDER BY turn_index",
                (session_id,)
            ).fetchall()

        summaries = []
        for r in rows:
            try:
                tool_calls = json.loads(r[5]) if r[5] else []
            except (json.JSONDecodeError, TypeError):
                tool_calls = []
            try:
                key_decisions = json.loads(r[6]) if r[6] else []
            except (json.JSONDecodeError, TypeError):
                key_decisions = []

            summaries.append(TurnSummary(
                turn_index=r[0],
                turn_type=r[1],
                summary=r[2],
                tokens_before=r[3],
                tokens_after=r[4],
                tool_calls=tool_calls,
                key_decisions=key_decisions,
                timestamp=r[7],
            ))
        return summaries

    def _save_compacted_context(
        self,
        *,
        session_id: str,
        original: int,
        compacted: int,
        tokens_before: int,
        tokens_after: int,
        data: dict,
    ) -> None:
        """保存 compacted context 元数据（仅限关键字参数调用）。"""
        with contextlib.closing(self._get_conn()) as conn:
            conn.execute(
                "INSERT INTO compacted_contexts "
                "(session_id, original_turns, compacted_turns, "
                "total_tokens_before, total_tokens_after, context_data, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, original, compacted,
                    tokens_before, tokens_after,
                    json.dumps(data, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
