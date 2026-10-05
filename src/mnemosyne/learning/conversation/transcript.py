"""
Transcript parsing.

Accepts Claude Code session transcripts (JSONL), generic chat JSON
(`[{"role", "content"}]` or `{"messages": [...]}`), and plain text with
`User:` / `Assistant:` prefixes.

Two safety properties matter more than format coverage:

* Only what the *developer* wrote is a source of preferences. Tool
  results, command echoes and system reminders arrive with the "user"
  role in many transcript formats and are filtered out.
* Mnemosyne never learns from its own output. Context it injected is
  wrapped in `<mnemosyne-context>` tags and stripped here, which closes
  the feedback loop where served memories would be re-learned as new
  observations.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

CONTEXT_TAG = "mnemosyne-context"
_STRIP_BLOCKS = re.compile(
    r"<(?P<tag>mnemosyne-context|system-reminder|command-message|command-name|command-args|local-command-stdout|"
    r"local-command-stderr|user-prompt-submit-hook|bash-stdout|bash-stderr)\b[^>]*>[\s\S]*?</(?P=tag)>",
    re.IGNORECASE,
)
_UNCLOSED_CONTEXT = re.compile(r"<mnemosyne-context\b[^>]*>[\s\S]*$", re.IGNORECASE)
_NOISE_PREFIXES = (
    "Caveat: The messages below were generated",
    "[Request interrupted",
    "This session is being continued from a previous conversation",
    "<command-",
    "<local-command",
)
_PLAIN_ROLE = re.compile(r"^(?P<role>user|human|me|assistant|ai|claude|bot)\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    text: str
    index: int
    timestamp: Optional[str] = None


@dataclass
class Transcript:
    turns: list[Turn] = field(default_factory=list)
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    git_branch: Optional[str] = None

    @property
    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "user"]


def clean_user_text(text: str) -> str:
    text = _STRIP_BLOCKS.sub(" ", text)
    text = _UNCLOSED_CONTEXT.sub(" ", text)
    return text.strip()


def _content_text(content: Any) -> Optional[str]:
    """Text a human typed, or None for tool results / non-text payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None  # a tool's output, not the developer speaking
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        return "\n".join(texts) if texts else None
    return None


def _usable(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith(_NOISE_PREFIXES)


def parse_claude_code_jsonl(lines: Iterable[str]) -> Transcript:
    transcript = Transcript()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        transcript.session_id = transcript.session_id or entry.get("sessionId")
        transcript.cwd = transcript.cwd or entry.get("cwd")
        transcript.git_branch = transcript.git_branch or entry.get("gitBranch")
        kind = entry.get("type")
        if kind not in ("user", "assistant") or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        if kind == "user" and entry.get("toolUseResult") is not None:
            continue
        message = entry.get("message") or {}
        text = _content_text(message.get("content"))
        if text is None:
            continue
        if kind == "user":
            text = clean_user_text(text)
        if not _usable(text):
            continue
        transcript.turns.append(Turn(kind, text, len(transcript.turns), entry.get("timestamp")))
    return transcript


def parse_messages(messages: list[Any], *, session_id: Optional[str] = None) -> Transcript:
    transcript = Transcript(session_id=session_id)
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).lower()
        role = {"human": "user", "ai": "assistant", "model": "assistant"}.get(role, role)
        if role not in ("user", "assistant"):
            continue
        text = _content_text(message.get("content"))
        if text is None:
            continue
        if role == "user":
            text = clean_user_text(text)
        if _usable(text):
            transcript.turns.append(Turn(role, text, len(transcript.turns)))
    return transcript


def parse_plain_text(text: str, *, session_id: Optional[str] = None) -> Transcript:
    transcript = Transcript(session_id=session_id)
    role = "user"
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            if role == "user":
                body = clean_user_text(body)
            if _usable(body):
                transcript.turns.append(Turn(role, body, len(transcript.turns)))

    for line in text.splitlines():
        match = _PLAIN_ROLE.match(line)
        if match:
            flush()
            buffer = [line[match.end():]]
            role = "assistant" if match.group("role").lower() in ("assistant", "ai", "claude", "bot") else "user"
        else:
            buffer.append(line)
    flush()
    return transcript


def load_transcript(source: str | Path, *, session_id: Optional[str] = None) -> Transcript:
    """Load any supported transcript format from a file path."""
    path = Path(source).expanduser()
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_transcript_text(text, session_id=session_id or path.stem)


def parse_transcript_text(text: str, *, session_id: Optional[str] = None) -> Transcript:
    stripped = text.lstrip()
    if stripped.startswith("{") and "\n" in stripped.strip():
        transcript = parse_claude_code_jsonl(text.splitlines())
        if transcript.turns:
            transcript.session_id = transcript.session_id or session_id
            return transcript
    if stripped.startswith(("[", "{")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            return parse_messages(payload["messages"], session_id=session_id or payload.get("session_id"))
        if isinstance(payload, list):
            return parse_messages(payload, session_id=session_id)
        if isinstance(payload, dict) and payload.get("type") in ("user", "assistant"):
            transcript = parse_claude_code_jsonl([text])
            transcript.session_id = transcript.session_id or session_id
            return transcript
    return parse_plain_text(text, session_id=session_id)
