# -*- coding: utf-8 -*-
"""agent_evidence.py — AI 会话审计证据（第 4 证据通道，零依赖）

生态共识（vct / ai-credit / codeburn / ai-code-stats 同源）：各宿主把完整会话
JSONL 保存在本地，其中 Agent 工具调用（写文件）是"AI 改过什么"的唯一可信事实源。
局限（继承生态共识）：只统计 Agent 自动写文件；复制粘贴（AI 输出但手动粘贴）
无法捕获 —— 该类行为只能靠用户确认（--ai-files）补充。

支持三种宿主日志（auto 探测可用目录）：
  - reasonix：%APPDATA%/reasonix/archive/*.jsonl（Windows）或 ~/.reasonix/archive/
    · edit_file 返回 content 以 "edited <path>" 开头 → 可解析
    · write_file 返回纯文件内容（无路径头）→ 不可解析（计入 missed 统计）
  - codex：~/.codex/sessions/**/*.jsonl
    · function_call(apply_patch|write_file) → payload.arguments 解析路径
  - claude-code：~/.claude/projects/**/*.jsonl
    · assistant.tool_use(Write|Edit|MultiEdit) → input.file_path
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

EDIT_PROTO_RE = re.compile(r"^edited (.+?)(?:\r?\n|$)", re.MULTILINE)
WROTE_TO_RE = re.compile(r"wrote \d+ bytes? to (.+?)(?:\r?\n|$)")
CODEX_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)(?: \*\*\*)?$", re.MULTILINE)
_ARCHIVE_NAME_DATE_RE = re.compile(r"^(\d{8})-")

HOSTS = ("reasonix", "codex", "claude-code")


def _name_date_ts(name: str):
    """从 archive 文件名解析日期（20260807-...）→ epoch；失败 None。"""
    m = _ARCHIVE_NAME_DATE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").timestamp()
    except ValueError:
        return None


def _now_ts():
    return datetime.now().timestamp()


def _ts_in_window(ts_str: str, before: float, window_min: int) -> bool:
    """宽松解析 ISO 时间戳；解析失败视为超窗（宁可漏不可误标）。"""
    try:
        ts = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return before - dt.timestamp() <= window_min * 60
    except (ValueError, TypeError):
        return False


def _to_rel(path_str: str, project_root: Path) -> str | None:
    """绝对/相对路径 → 项目内相对 posix 路径；项目外返回 None。"""
    p = Path(path_str.strip().strip('"'))
    try:
        rel = p.resolve().relative_to(project_root.resolve())
    except (ValueError, OSError):
        if not path_str.strip() or path_str.strip().startswith(("/", "C:", "\\")):
            return None
        return path_str.strip().replace("\\", "/")  # 已是相对路径
    return rel.as_posix()


class AgentEvidence:
    """收集某时间窗内各宿主日志记录的 AI 写入文件。"""

    def __init__(self, project_root: Path, window_min: int = 120):
        self.project_root = Path(project_root)
        self.window_min = window_min
        self.files: dict[str, str] = {}   # rel_path -> host
        self.missed_write_file = 0         # reasonix write_file 无法解析计数
        self.logs_scanned = 0

    def _record(self, path_str: str, host: str):
        rel = _to_rel(path_str, self.project_root)
        if rel:
            self.files[rel] = host

    # ---------- reasonix ----------
    def scan_reasonix(self, archive_dir: Path):
        if not archive_dir.is_dir():
            return
        before = _now_ts()
        for p in sorted(archive_dir.glob("*.jsonl")):
            self.logs_scanned += 1
            # archive 行无时间戳（role/content/tool_call_id/name）：
            # 以文件级时间近似（mtime 与文件名日期取较早者），会话粒度过滤。
            try:
                file_ts = min(p.stat().st_mtime, _name_date_ts(p.name) or p.stat().st_mtime)
            except OSError:
                continue
            if before - file_ts > self.window_min * 60:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if ev.get("role") != "tool" or not isinstance(ev.get("content"), str):
                    continue
                content = ev["content"]
                m = EDIT_PROTO_RE.search(content)
                if m:
                    self._record(m.group(1), "reasonix")
                elif WROTE_TO_RE.search(content):
                    self._record(WROTE_TO_RE.search(content).group(1), "reasonix")
                elif content and not content.startswith(("edited ", "wrote ")):
                    self.missed_write_file += 1  # write_file 返回纯内容，无法取路径
        return self

    # ---------- codex ----------
    def scan_codex(self, sessions_dir: Path):
        if not sessions_dir.is_dir():
            return
        before = _now_ts()
        for p in sorted(sessions_dir.rglob("*.jsonl")):
            self.logs_scanned += 1
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "function_call":
                    continue
                payload = ev.get("payload") or {}
                name = payload.get("name", "")
                if name not in ("apply_patch", "write_file"):
                    continue
                ts = str(ev.get("createdAt") or ev.get("timestamp") or "")
                if not _ts_in_window(ts, before, self.window_min):
                    continue
                args = payload.get("arguments", "")
                paths = set()
                if isinstance(args, dict):
                    paths.add(str(args.get("file_path", args.get("path", ""))))
                else:
                    text = str(args)
                    paths |= set(CODEX_FILE_RE.findall(text))
                    m = re.search(r"file_path[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", text)
                    if m:
                        paths.add(m.group(1))
                for ps in paths:
                    if ps:
                        self._record(ps, "codex")
        return self

    # ---------- claude-code ----------
    def scan_claude_code(self, projects_dir: Path):
        if not projects_dir.is_dir():
            return
        before = _now_ts()
        for p in sorted(projects_dir.rglob("*.jsonl")):
            self.logs_scanned += 1
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if not _ts_in_window(str(ev.get("timestamp", "")), before, self.window_min):
                    continue
                msg = ev.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                        continue
                    fp = (block.get("input") or {}).get("file_path")
                    if fp:
                        self._record(str(fp), "claude-code")
        return self

    # ---------- auto ----------
    def scan_all(self, hosts: tuple = HOSTS, home: Path | None = None):
        home = home or Path.home()
        if "reasonix" in hosts:
            candidates = []
            appdata = os.environ.get("APPDATA")
            if appdata:
                candidates.append(Path(appdata) / "reasonix" / "archive")
            candidates.append(home / ".reasonix" / "archive")
            for c in candidates:
                self.scan_reasonix(c)
        if "codex" in hosts:
            self.scan_codex(home / ".codex" / "sessions")
        if "claude-code" in hosts:
            self.scan_claude_code(home / ".claude" / "projects")
        return self

    def report(self) -> dict:
        return {
            "files": self.files,
            "hosts": sorted(set(self.files.values())),
            "logs_scanned": self.logs_scanned,
            "missed_write_file": self.missed_write_file,
        }


if __name__ == "__main__":
    # 快速自检：python scripts/agent_evidence.py <project-root>
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    ev = AgentEvidence(Path(root)).scan_all()
    r = ev.report()
    print(f"日志扫描 {r['logs_scanned']} 份；AI 写入文件 {len(r['files'])} 个（宿主: {r['hosts']}）")
    for pth, host in sorted(r["files"].items()):
        print(f"  [{host}] {pth}")
