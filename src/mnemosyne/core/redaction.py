"""
Secret redaction.

Everything Mnemosyne learns passes through here before it is stored.
Conversations with coding assistants routinely contain pasted
credentials — connection strings, API keys, tokens in stack traces — and
a memory store that quietly persisted them would turn into the most
valuable target on a developer's machine. Redaction is therefore not
optional and not configurable off.

Detection combines provider-specific token formats (high precision),
credential-looking assignments (`password = ...`), credentials embedded
in URLs, PEM private keys, and a conservative high-entropy fallback for
unknown token formats.
"""
from __future__ import annotations

import functools
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
        r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----|$)"
    )),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}\b")),
    ("mnemosyne_key", re.compile(r"\bmn_[a-f0-9]{12}_[A-Za-z0-9_\-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=+/]{20,}")),
]

#: `password = "hunter2hunter2"`, `API_KEY: abc...`, `--token=...`
_ASSIGNMENT = re.compile(
    r"(?i)(?P<name>\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|client[_-]?secret"
    r"|private[_-]?key|auth[_-]?key|credentials?)[\w.-]*)(?P<sep>\s*[:=]\s*|\s+)(?P<quote>['\"]?)"
    r"(?P<value>[^\s'\"]{6,})(?P=quote)"
)
#: `postgres://user:secret@host` -> keep scheme/user/host, drop the password.
_URL_CREDENTIALS = re.compile(r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)(?P<password>[^@/\s]{3,})(?P<at>@)")
_CANDIDATE_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_HEX = re.compile(r"^[0-9a-f]+$")
_PLACEHOLDER_VALUES = frozenset(
    {"changeme", "password", "example", "xxxxxx", "redacted", "<redacted>", "your-api-key", "your_api_key",
     "placeholder", "secret", "null", "none", "true", "false", "os.environ", "settings"}
)


@dataclass
class RedactionResult:
    text: str
    findings: list[str] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


def _entropy(token: str) -> float:
    counts = Counter(token)
    total = len(token)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _looks_random(token: str) -> bool:
    """Conservative: long, mixed-class, high-entropy strings. Pure hex up
    to 64 chars is left alone (commit SHAs and content hashes are useful
    and not secret)."""
    if len(token) < 32:
        return False
    if _HEX.match(token.lower()) and len(token) <= 64:
        return False
    classes = sum(
        (any(c.islower() for c in token), any(c.isupper() for c in token), any(c.isdigit() for c in token))
    )
    if classes < 3:
        return False
    if "/" in token and token.count("/") > 2:  # file paths, URLs
        return False
    return _entropy(token) >= 4.2


def _plausible_secret(value: str, *, loose_separator: bool) -> bool:
    """Separate `password = hunter2hunter2` from prose ("the token expired")
    and code (`api_key: Optional[str]`)."""
    value = value.strip(",;")
    if any(ch in value for ch in "[](){}<>"):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    mixed_case = any(c.islower() for c in value) and any(c.isupper() for c in value)
    has_symbol = any(c in "-_./+=!@#%^&*" for c in value)
    if loose_separator:
        return len(value) >= 8 and has_digit and has_alpha
    return len(value) >= 6 and (has_digit or mixed_case or has_symbol or len(value) >= 16)


def _mark_match(mark: Callable[[str], str], kind: str, _match: re.Match[str]) -> str:
    return mark(kind)


def redact(text: str) -> RedactionResult:
    if not text:
        return RedactionResult(text or "")
    findings: list[str] = []

    def mark(kind: str) -> str:
        findings.append(kind)
        return f"[REDACTED:{kind}]"

    for kind, pattern in _PATTERNS:
        text = pattern.sub(functools.partial(_mark_match, mark, kind), text)

    def _assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        if value.startswith("[REDACTED") or value.lower().strip("$<>{}()") in _PLACEHOLDER_VALUES:
            return match.group(0)
        if value.startswith(("$", "${", "os.environ", "process.env", "env(")):
            return match.group(0)  # a reference to a secret, not the secret
        loose = not match.group("sep").strip()  # "token expired" vs "token = ..."
        if not _plausible_secret(value, loose_separator=loose):
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('name')}{match.group('sep')}{quote}{mark('credential')}{quote}"

    text = _ASSIGNMENT.sub(_assignment, text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('prefix')}{mark('url_password')}{m.group('at')}", text)
    text = _CANDIDATE_TOKEN.sub(lambda m: mark("high_entropy") if _looks_random(m.group(0)) else m.group(0), text)
    return RedactionResult(text=text, findings=findings)


_MARKER = re.compile(r"\[REDACTED:[a-z_]+\]")


def remaining_substance(redacted: str) -> int:
    """How many meaningful words survive redaction. A candidate that is
    little more than a secret ("key: AKIA...") has nothing worth keeping."""
    from mnemosyne.core.text import tokenize

    return len(tokenize(_MARKER.sub(" ", redacted)))
