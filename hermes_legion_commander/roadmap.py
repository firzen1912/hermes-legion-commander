"""Deterministic Markdown roadmap scoping and version-entry parsing.

This module is intentionally local-only: it performs no subprocess, network, worker CLI,
or provider calls. Both council and checkpoint orchestration use it before agents launch.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


DEFAULT_IMPLEMENTATION_SECTION = "version-by-version implementation"

_HEADING_RE = re.compile(r"(?m)^(?P<marks>#{1,6})[ \t]+(?P<title>[^\n]+?)[ \t]*$")
_VERSION_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})[ \t]+"
    r"v(?P<version>\d+(?:\.\d+)?[A-Za-z]?)"
    r"(?:[ \t]*/[ \t]*(?P<release>\d+\.\d+\.\d+))?"
    r"(?:[ \t]*(?:(?:[-\u2013\u2014:|])[ \t]*|[ \t]+)(?P<title>.*?))?"
    r"[ \t]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MarkdownSection:
    heading: str | None
    level: int | None
    start: int
    end: int
    text: str
    found: bool


@dataclass(frozen=True)
class VersionEntry:
    version: str
    release_version: str | None
    title: str
    heading: str
    level: int
    start: int
    end: int
    text: str

    @property
    def numeric_base(self) -> int:
        match = re.match(r"\d+", self.version)
        if not match:  # pragma: no cover - guarded by parser regex
            raise ValueError(f"version has no numeric base: {self.version}")
        return int(match.group(0))


def normalize_markdown(markdown: str) -> str:
    """Normalize CRLF, lone CR, and an optional UTF-8 BOM for deterministic parsing."""
    return markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _normalized_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _matches_section_heading(title: str, section_name: str) -> bool:
    normalized = _normalized_heading(title)
    needle = _normalized_heading(section_name)
    return normalized == needle or normalized.startswith(needle + " ") or normalized.startswith(needle + "(")


def implementation_section(
    markdown: str,
    section_name: str = DEFAULT_IMPLEMENTATION_SECTION,
) -> MarkdownSection:
    """Return the named implementation section, or the full document as a fallback.

    Parsing is newline-agnostic. The section ends at the next Markdown heading at the
    same or a higher level. Matching accepts a range suffix such as ``(v12-v100)``.
    """
    normalized_markdown = normalize_markdown(markdown)
    headings = list(_HEADING_RE.finditer(normalized_markdown))
    for index, match in enumerate(headings):
        title = match.group("title").strip()
        if not _matches_section_heading(title, section_name):
            continue
        level = len(match.group("marks"))
        end = len(normalized_markdown)
        for later in headings[index + 1 :]:
            if len(later.group("marks")) <= level:
                end = later.start()
                break
        return MarkdownSection(
            heading=title,
            level=level,
            start=match.start(),
            end=end,
            text=normalized_markdown[match.start():end].strip(),
            found=True,
        )
    return MarkdownSection(
        heading=None,
        level=None,
        start=0,
        end=len(normalized_markdown),
        text=normalized_markdown.strip(),
        found=False,
    )


def parse_version_entries(
    markdown: str,
    section_name: str = DEFAULT_IMPLEMENTATION_SECTION,
) -> tuple[MarkdownSection, tuple[VersionEntry, ...]]:
    """Parse actual version headings from the implementation spine.

    When the named implementation spine exists, only its direct child headings are
    accepted. This excludes incidental semantic versions in prose and nested headings.
    For simpler roadmaps without a named spine, the shallowest version-heading level is
    used as a backward-compatible fallback.
    """
    section = implementation_section(markdown, section_name)
    candidates: list[tuple[re.Match[str], re.Match[str]]] = []
    for heading_match in _HEADING_RE.finditer(section.text):
        raw_line = heading_match.group(0).strip()
        version_match = _VERSION_HEADING_RE.match(raw_line)
        if version_match:
            candidates.append((heading_match, version_match))

    if section.found and section.level is not None:
        required_level = section.level + 1
        candidates = [
            item for item in candidates
            if len(item[1].group("marks")) == required_level
        ]
    elif candidates:
        shallowest = min(len(item[1].group("marks")) for item in candidates)
        candidates = [
            item for item in candidates
            if len(item[1].group("marks")) == shallowest
        ]

    entries: list[VersionEntry] = []
    for index, (heading_match, version_match) in enumerate(candidates):
        start = heading_match.start()
        end = candidates[index + 1][0].start() if index + 1 < len(candidates) else len(section.text)
        entries.append(
            VersionEntry(
                version=version_match.group("version"),
                release_version=version_match.group("release"),
                title=(version_match.group("title") or "").strip(),
                heading=heading_match.group("title").strip(),
                level=len(version_match.group("marks")),
                start=start,
                end=end,
                text=section.text[start:end].strip(),
            )
        )
    return section, tuple(entries)


def version_keys(entries: tuple[VersionEntry, ...]) -> list[str]:
    return [entry.version for entry in entries]


def release_versions(entries: tuple[VersionEntry, ...]) -> list[str]:
    return [entry.release_version for entry in entries if entry.release_version]


def campaign_versions(entries: tuple[VersionEntry, ...]) -> list[int]:
    """Return unique integer campaign phases in document order."""
    seen: set[int] = set()
    result: list[int] = []
    for entry in entries:
        value = entry.numeric_base
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_version_entry(markdown: str, version: int | str) -> str:
    """Extract an exact roadmap phase, grouping lettered variants for integer phases.

    ``28`` groups ``v28a``, ``v28b``, and ``v28c`` when no exact ``v28`` heading exists.
    ``40`` resolves the exact ``v40`` entry and does not silently include patch ``v40.1``.
    """
    requested = str(version).strip().lower().removeprefix("v")
    _, entries = parse_version_entries(markdown)
    exact = [entry for entry in entries if entry.version.casefold() == requested.casefold()]
    if exact:
        return "\n\n".join(entry.text for entry in exact)
    if requested.isdigit():
        variants = [
            entry for entry in entries
            if entry.numeric_base == int(requested)
            and re.fullmatch(rf"{re.escape(requested)}[A-Za-z]+", entry.version, re.IGNORECASE)
        ]
        if variants:
            return "\n\n".join(entry.text for entry in variants)
    raise KeyError(f"roadmap has no implementation entry for v{requested}")


def extract_version_range(markdown: str, start: int, end: int) -> str:
    """Extract only version headings whose numeric base is within the requested range."""
    _, entries = parse_version_entries(markdown)
    selected = [entry.text for entry in entries if start <= entry.numeric_base <= end]
    if not selected:
        raise KeyError(f"roadmap has no implementation entries for v{start}-v{end}")
    prefix = f"## Selected version-by-version implementation (v{start}-v{end})\n\n"
    return prefix + "\n\n".join(selected)
