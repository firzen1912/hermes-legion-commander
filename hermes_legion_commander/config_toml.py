"""TOML loading helpers for operator-authored Commander configs."""
from __future__ import annotations

import re
import tomllib
from typing import Any

_WINDOWS_DRIVE_PATH = re.compile(r"(?:^|[^A-Za-z0-9_])[A-Za-z]:\\")


def _looks_like_windows_path(value: str) -> bool:
    return bool(_WINDOWS_DRIVE_PATH.search(value) or value.startswith("\\\\"))


def _escape_windows_backslashes(value: str) -> str:
    """Make raw Windows paths parse as TOML basic strings.

    TOML requires backslashes in double-quoted strings to be escaped, but Windows
    users commonly write values such as ``repo = "C:\\project"``. Without this
    repair, ``\\U`` can fail parsing and ``\\t`` can silently become a tab.
    Already-escaped backslash pairs are preserved.
    """
    out: list[str] = []
    i = 0
    if value.startswith("\\\\") and not value.startswith("\\\\\\\\"):
        out.append("\\\\\\\\")
        i = 2
    while i < len(value):
        ch = value[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 < len(value) and value[i + 1] == "\\":
            out.append("\\\\")
            i += 2
            continue
        out.append("\\\\")
        i += 1
    return "".join(out)


def _escape_windows_paths_in_basic_strings(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            if end == -1:
                out.append(text[i:])
                break
            out.append(text[i:end + 3])
            i = end + 3
            continue
        if text[i] != '"':
            out.append(text[i])
            i += 1
            continue
        start = i
        i += 1
        escaped = False
        while i < len(text):
            ch = text[i]
            if ch == '"' and not escaped:
                break
            escaped = (ch == "\\" and not escaped)
            if ch != "\\":
                escaped = False
            i += 1
        if i >= len(text):
            out.append(text[start:])
            break
        value = text[start + 1:i]
        if _looks_like_windows_path(value):
            value = _escape_windows_backslashes(value)
        out.append('"' + value + '"')
        i += 1
    return "".join(out)


def loads(text: str) -> dict[str, Any]:
    """Load Commander TOML, accepting common raw Windows path strings."""
    return tomllib.loads(_escape_windows_paths_in_basic_strings(text))
