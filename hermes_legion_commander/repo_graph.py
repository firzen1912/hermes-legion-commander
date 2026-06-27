"""Repository knowledge graph and context-pack generation for worker prompts.

The graph layer is intentionally local-first.  It borrows the useful workflow of
Graphify-style code maps—persistent graph JSON, a report, an interactive HTML
view, cache-backed rebuilds, communities, path/query helpers, and compact
assistant context packs—without requiring source code to leave the workstation.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import tomllib
from collections import Counter, deque
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
GRAPH_SCHEMA_VERSION = 2
PARSER_VERSION = 2

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env", "node_modules",
    "__pycache__", "build", "dist", "site-packages", ".eggs", ".cache",
    "htmlcov", "coverage", ".coverage", ".next", ".nuxt", "target", "out",
    ".gradle", ".terraform", ".serverless", ".parcel-cache", ".turbo",
}

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".tgz", ".bz2", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class", ".jar", ".whl", ".sqlite", ".db", ".mp4", ".mov",
    ".m4v", ".avi", ".mp3", ".wav", ".flac", ".woff", ".woff2", ".ttf", ".otf",
}

MULTIMODAL_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mov", ".mp3", ".wav",
}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".txt": "text",
    ".toml": "toml",
    ".json": "json",
    ".jsonl": "jsonl",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c-header",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp-header",
    ".cs": "csharp",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
}

SOURCE_EXTS = set(LANGUAGE_BY_EXT) | {""}
DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
CONFIG_NAMES = {
    "pyproject.toml", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock", "dockerfile", "compose.yaml",
    "docker-compose.yml", "makefile", "justfile", "tox.ini", "ruff.toml", ".pre-commit-config.yaml",
}
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "have", "has", "had",
    "can", "will", "would", "should", "could", "task", "request", "please", "make",
    "correct", "based", "data", "model", "effort", "usage", "token", "tokens", "repo",
    "repository", "file", "files", "code", "cli", "stage", "worker", "runtime",
    "add", "update", "fix", "change", "implement", "create", "ensure", "want",
}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relpath(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def language_for(path: Path) -> str:
    lower_name = path.name.lower()
    if lower_name == "dockerfile" or lower_name.endswith(".dockerfile"):
        return "dockerfile"
    return LANGUAGE_BY_EXT.get(path.suffix.lower(), "text")


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _is_probably_text(path: Path) -> bool:
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    if suffix in MULTIMODAL_EXTS:
        return True
    if suffix in BINARY_EXTS:
        return False
    if suffix in SOURCE_EXTS or suffix in DOC_EXTS:
        return True
    return lower_name in CONFIG_NAMES or path.name in {"LICENSE", "NOTICE", "CHANGELOG", "README"}


def iter_repo_files(repo: Path, *, max_files: int = 1500, max_bytes: int = 1_500_000) -> list[Path]:
    repo = repo.resolve()
    files: list[Path] = []
    if not repo.is_dir():
        return files
    for path in sorted(repo.rglob("*")):
        if len(files) >= max_files:
            break
        try:
            relative = path.relative_to(repo)
        except ValueError:
            continue
        if path.is_dir() or _is_skipped(relative):
            continue
        if not path.is_file() or not _is_probably_text(path):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def _safe_read(path: Path, *, max_chars: int = 260_000) -> str:
    if path.suffix.lower() in MULTIMODAL_EXTS:
        return ""
    try:
        data = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    return data[:max_chars]


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text else 0)


def _module_name(repo: Path, path: Path) -> str | None:
    if path.suffix.lower() != ".py":
        return None
    parts = list(path.relative_to(repo).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _python_module_index(repo: Path, files: list[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in files:
        module = _module_name(repo, path)
        if module:
            result[module] = relpath(repo, path)
    return result


def _resolve_import(module: str, module_index: dict[str, str]) -> str | None:
    if not module:
        return None
    probe = module
    while probe:
        if probe in module_index:
            return module_index[probe]
        if "." not in probe:
            break
        probe = probe.rsplit(".", 1)[0]
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return _call_name(node)


def _annotation_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    for arg in list(node.args.posonlyargs) + list(node.args.args):
        annotation = _annotation_text(arg.annotation)
        args.append(f"{arg.arg}: {annotation}" if annotation else arg.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    for arg in node.args.kwonlyargs:
        annotation = _annotation_text(arg.annotation)
        args.append(f"{arg.arg}: {annotation}" if annotation else arg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    returns = _annotation_text(node.returns)
    return f"{node.name}({', '.join(args)})" + (f" -> {returns}" if returns else "")


class _PythonAnalyzer(ast.NodeVisitor):
    def __init__(self, text: str) -> None:
        self.text = text
        self.imports: list[str] = []
        self.import_aliases: dict[str, str] = {}
        self.symbols: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._stack: list[str] = []
        self._current_symbol: str | None = None

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.imports.append(alias.name)
            self.import_aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        base = "." * int(node.level or 0) + (node.module or "")
        if node.module:
            self.imports.append(node.module)
        for alias in node.names:
            if node.module:
                self.import_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif base:
                self.import_aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        qualname = ".".join(self._stack + [node.name])
        symbol = {
            "name": node.name,
            "kind": "class",
            "line": getattr(node, "lineno", None),
            "end_line": getattr(node, "end_lineno", None),
            "qualname": qualname,
            "doc": (ast.get_docstring(node) or "")[:240],
            "decorators": [x for x in (_decorator_name(d) for d in node.decorator_list) if x],
            "bases": [x for x in (_annotation_text(b) for b in node.bases) if x],
        }
        self.symbols.append(symbol)
        self._stack.append(node.name)
        prior = self._current_symbol
        self._current_symbol = qualname
        for child in node.body:
            self.visit(child)
        self._current_symbol = prior
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node, "method" if self._stack else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node, "async_method" if self._stack else "async_function")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> Any:
        qualname = ".".join(self._stack + [node.name])
        complexity = 1 + sum(isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.BoolOp, ast.Match)) for n in ast.walk(node))
        symbol = {
            "name": node.name,
            "kind": kind,
            "line": getattr(node, "lineno", None),
            "end_line": getattr(node, "end_lineno", None),
            "qualname": qualname,
            "doc": (ast.get_docstring(node) or "")[:240],
            "decorators": [x for x in (_decorator_name(d) for d in node.decorator_list) if x],
            "signature": _signature(node),
            "complexity_hint": int(complexity),
        }
        self.symbols.append(symbol)
        self._stack.append(node.name)
        prior = self._current_symbol
        self._current_symbol = qualname
        for child in node.body:
            self.visit(child)
        self._current_symbol = prior
        self._stack.pop()

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        if name:
            self.calls.append({
                "caller": self._current_symbol,
                "callee": name,
                "line": getattr(node, "lineno", None),
            })
        self.generic_visit(node)


def _parse_python(repo: Path, path: Path, text: str, module_index: dict[str, str]) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {"parse_error": f"SyntaxError: {exc.msg}", "imports": [], "resolved_imports": [], "symbols": [], "calls": []}
    analyzer = _PythonAnalyzer(text)
    analyzer.visit(tree)
    resolved_imports: list[str] = []
    for module in sorted(set(analyzer.imports)):
        resolved = _resolve_import(module, module_index)
        if resolved and resolved != relpath(repo, path):
            resolved_imports.append(resolved)
    symbol_names = {str(s.get("name")): str(s.get("qualname")) for s in analyzer.symbols if s.get("name")}
    symbol_qualnames = {str(s.get("qualname")) for s in analyzer.symbols if s.get("qualname")}
    calls: list[dict[str, Any]] = []
    for call in analyzer.calls[:800]:
        callee = str(call.get("callee") or "")
        resolved_symbol = None
        if callee in symbol_names:
            resolved_symbol = symbol_names[callee]
        elif callee.rsplit(".", 1)[-1] in symbol_names:
            resolved_symbol = symbol_names[callee.rsplit(".", 1)[-1]]
        elif callee in symbol_qualnames:
            resolved_symbol = callee
        calls.append({**call, "resolved_symbol": resolved_symbol})
    return {
        "imports": sorted(set(analyzer.imports))[:160],
        "import_aliases": dict(sorted(analyzer.import_aliases.items())) ,
        "resolved_imports": sorted(set(resolved_imports))[:160],
        "symbols": sorted(analyzer.symbols, key=lambda row: (row.get("line") or 0, row.get("name", "")))[:260],
        "calls": calls[:800],
        "doc": (ast.get_docstring(tree) or "")[:320],
    }


def _parse_markdown(text: str) -> dict[str, Any]:
    headings: list[dict[str, Any]] = []
    refs: list[str] = []
    links: list[dict[str, Any]] = []
    code_fences: Counter[str] = Counter()
    for lineno, line in enumerate(text.splitlines(), 1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            headings.append({"level": len(heading.group(1)), "title": heading.group(2)[:180], "line": lineno})
        fence = re.match(r"^```([A-Za-z0-9_+.-]+)?", line.strip())
        if fence and fence.group(1):
            code_fences[fence.group(1).lower()] += 1
        for match in re.finditer(r"`([^`]+\.(?:py|md|toml|json|yaml|yml|ts|tsx|js|jsx|go|rs|sh|ps1|sql))`", line):
            refs.append(match.group(1))
        for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", line):
            target = match.group(2).strip()
            links.append({"text": match.group(1)[:120], "target": target[:240], "line": lineno})
            if re.search(r"\.(py|md|toml|json|yaml|yml|ts|tsx|js|jsx|go|rs|sh|ps1|sql)(#.*)?$", target):
                refs.append(target.split("#", 1)[0])
    return {"headings": headings[:140], "path_refs": sorted(set(refs))[:140], "links": links[:160], "code_fences": dict(code_fences.most_common(20))}


def _parse_toml(path: Path, text: str) -> dict[str, Any]:
    sections: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if match:
            sections.append(match.group(1))
    entrypoints: dict[str, str] = {}
    deps: list[str] = []
    if path.name == "pyproject.toml":
        try:
            obj = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            obj = {}
        project = (obj.get("project") or {}) if isinstance(obj, dict) else {}
        scripts = (project.get("scripts") or {}) if isinstance(project, dict) else {}
        if isinstance(scripts, dict):
            entrypoints = {str(k): str(v) for k, v in scripts.items()}
        dependencies = (project.get("dependencies") or []) if isinstance(project, dict) else []
        if isinstance(dependencies, list):
            deps.extend(str(x).split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip() for x in dependencies)
    return {"sections": sections[:120], "entrypoints": entrypoints, "dependencies": sorted({x for x in deps if x})[:120]}


def _parse_json_like(text: str) -> dict[str, Any]:
    keys: list[str] = []
    path_refs: list[str] = []
    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    def walk(value: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_s = str(key)
                keys.append(f"{prefix}.{key_s}" if prefix else key_s)
                walk(child, f"{prefix}.{key_s}" if prefix else key_s)
        elif isinstance(value, list):
            for child in value[:80]:
                walk(child, prefix)
        elif isinstance(value, str):
            if re.search(r"\.(py|md|toml|json|yaml|yml|ts|tsx|js|jsx|go|rs|sh|ps1|sql)$", value):
                path_refs.append(value)
    if obj is not None:
        walk(obj)
    else:
        for match in re.finditer(r"['\"]([A-Za-z0-9_.-]+)['\"]\s*:", text):
            keys.append(match.group(1))
    return {"sections": keys[:160], "path_refs": sorted(set(path_refs))[:120]}


def _parse_yaml_like(text: str) -> dict[str, Any]:
    keys: list[str] = []
    path_refs: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", line)
        if match:
            keys.append(match.group(1))
            value = match.group(2).strip().strip("'\"")
            if re.search(r"\.(py|md|toml|json|yaml|yml|ts|tsx|js|jsx|go|rs|sh|ps1|sql)$", value):
                path_refs.append(value)
    return {"sections": keys[:160], "path_refs": sorted(set(path_refs))[:120]}


def _parse_sql(text: str) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    refs: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        create = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|INDEX|TRIGGER|FUNCTION)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`]+)", line, re.I)
        if create:
            symbols.append({"name": create.group(1).strip('"`'), "kind": "sql_object", "line": lineno, "qualname": create.group(1).strip('"`')})
        for match in re.finditer(r"\b(?:FROM|JOIN|REFERENCES|INTO|UPDATE)\s+([\w.\"`]+)", line, re.I):
            refs.append(match.group(1).strip('"`'))
    return {"symbols": symbols[:160], "imports": sorted(set(refs))[:160], "resolved_imports": []}


def _resolve_relative_import(repo: Path, source: Path, spec: str, candidates: list[str]) -> str | None:
    if not spec.startswith("."):
        return None
    base = (source.parent / spec).resolve()
    probes = [base]
    for ext in candidates:
        probes.append(Path(str(base) + ext))
        probes.append(base / f"index{ext}")
    for probe in probes:
        if probe.is_file():
            return relpath(repo, probe)
    return None


def _parse_ecmascript(repo: Path, path: Path, text: str) -> dict[str, Any]:
    imports: list[str] = []
    resolved_imports: list[str] = []
    symbols: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in re.finditer(r"\bfrom\s+['\"]([^'\"]+)['\"]|\bimport\(['\"]([^'\"]+)['\"]\)|\brequire\(['\"]([^'\"]+)['\"]\)", line):
            spec = next(x for x in match.groups() if x)
            imports.append(spec)
            resolved = _resolve_relative_import(repo, path, spec, [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"])
            if resolved:
                resolved_imports.append(resolved)
        fn = re.search(r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|\b(?:export\s+)?class\s+([A-Za-z_$][\w$]*)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*?\)?\s*=>", line)
        if fn:
            name = next(x for x in fn.groups() if x)
            kind = "class" if "class" in line else "function"
            symbols.append({"name": name, "kind": kind, "line": lineno, "qualname": name})
        for call in re.finditer(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(", line):
            name = call.group(1)
            if name not in {"if", "for", "while", "switch", "catch", "function"}:
                calls.append({"caller": None, "callee": name, "line": lineno, "resolved_symbol": None})
    return {"imports": sorted(set(imports))[:160], "symbols": symbols[:220], "resolved_imports": sorted(set(resolved_imports))[:160], "calls": calls[:500]}


def _parse_go_rust_java_like(text: str, language: str) -> dict[str, Any]:
    imports: list[str] = []
    symbols: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if language == "go":
            imp = re.search(r"^\s*import\s+(?:[\w.]+\s+)?\"([^\"]+)\"", line)
            if imp:
                imports.append(imp.group(1))
            fn = re.search(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)", line)
            if fn:
                symbols.append({"name": fn.group(1), "kind": "function", "line": lineno, "qualname": fn.group(1)})
            typ = re.search(r"^\s*type\s+([A-Za-z_][\w]*)\s+", line)
            if typ:
                symbols.append({"name": typ.group(1), "kind": "type", "line": lineno, "qualname": typ.group(1)})
        elif language == "rust":
            imp = re.search(r"^\s*use\s+([^;]+);", line)
            if imp:
                imports.append(imp.group(1).strip())
            sym = re.search(r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)", line)
            if sym:
                symbols.append({"name": sym.group(1), "kind": "symbol", "line": lineno, "qualname": sym.group(1)})
        else:
            imp = re.search(r"^\s*import\s+([A-Za-z0-9_.]+);", line)
            if imp:
                imports.append(imp.group(1))
            sym = re.search(r"\b(?:class|interface|enum|record)\s+([A-Za-z_][\w]*)", line)
            if sym:
                symbols.append({"name": sym.group(1), "kind": "type", "line": lineno, "qualname": sym.group(1)})
            meth = re.search(r"\b(?:public|private|protected|static|final|synchronized|async|override|internal|sealed|virtual|extern|new|partial|\s)+[A-Za-z0-9_<>,?\[\]]+\s+([A-Za-z_][\w]*)\s*\(", line)
            if meth and meth.group(1) not in {"if", "for", "while", "switch"}:
                symbols.append({"name": meth.group(1), "kind": "method", "line": lineno, "qualname": meth.group(1)})
    return {"imports": sorted(set(imports))[:160], "symbols": symbols[:220], "resolved_imports": []}


def _parse_shell_like(text: str, language: str) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        fn = re.search(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{|^\s*function\s+([A-Za-z_][\w-]*)", line)
        if fn:
            name = next(x for x in fn.groups() if x)
            symbols.append({"name": name, "kind": "function", "line": lineno, "qualname": name})
        dot = re.search(r"^\s*(?:source|\.)\s+(.+)$", line)
        if dot:
            imports.append(dot.group(1).strip().strip('"\''))
        ps = re.search(r"^\s*function\s+([A-Za-z_][\w-]*)", line, re.I)
        if language == "powershell" and ps:
            symbols.append({"name": ps.group(1), "kind": "function", "line": lineno, "qualname": ps.group(1)})
    return {"imports": sorted(set(imports))[:160], "symbols": symbols[:160], "resolved_imports": []}


def _parse_dockerfile(text: str) -> dict[str, Any]:
    sections: list[str] = []
    imports: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instr = stripped.split(None, 1)[0].upper()
        sections.append(instr)
        if instr == "FROM" and len(stripped.split(None, 1)) > 1:
            imports.append(stripped.split(None, 1)[1].split()[0])
    return {"sections": sections[:120], "imports": imports[:80], "resolved_imports": []}


def _file_kind(relative: str, language: str) -> str:
    lower = relative.lower()
    name = Path(lower).name
    if "/test" in lower or lower.startswith("tests/") or re.search(r"(^|/)(test_|.*_test\.)", lower) or "/__tests__/" in lower:
        return "test"
    if language in {"markdown", "restructuredtext"} or lower.startswith("docs/") or lower.startswith("request/"):
        return "docs"
    if language == "sql" or lower.startswith(("migrations/", "schema/", "db/")):
        return "schema"
    if lower.startswith("scripts/") or lower.endswith((".sh", ".ps1", ".bash", ".zsh")):
        return "script"
    if language in {"yaml", "toml", "json", "dockerfile", "terraform"} or name in CONFIG_NAMES or lower.startswith("config/") or lower.startswith(".github/"):
        return "config"
    if Path(lower).suffix in MULTIMODAL_EXTS:
        return "asset"
    return "source" if language != "text" else "text"


def _word_tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_./-]{2,}", text)
    expanded: list[str] = []
    for token in raw:
        expanded.append(token.lower())
        for part in re.split(r"[_.:/\-]+", token):
            if len(part) >= 3:
                expanded.append(part.lower())
        # Split simple camelCase/PascalCase boundaries.
        camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token)
        if camel != token:
            expanded.extend(part.lower() for part in camel.split() if len(part) >= 3)
    return expanded


def _extract_terms(text: str, *, limit: int = 40) -> list[str]:
    tokens = _word_tokens(text)
    counts = Counter(t for t in tokens if t not in STOPWORDS and not t.startswith("http"))
    return [term for term, _ in counts.most_common(limit)]


def _path_ref_to_file(repo: Path, source: Path, ref: str, path_set: set[str]) -> str | None:
    ref = ref.strip().strip("'\"")
    if not ref or ref.startswith(("http://", "https://", "mailto:")):
        return None
    ref = ref.split("#", 1)[0]
    probes: list[Path] = []
    if ref.startswith("/"):
        probes.append(repo / ref.lstrip("/"))
    else:
        probes.append(source.parent / ref)
        probes.append(repo / ref)
    for probe in probes:
        try:
            relative = relpath(repo, probe.resolve())
        except OSError:
            continue
        if relative in path_set:
            return relative
    normalized = ref.replace("\\", "/").lstrip("./")
    return normalized if normalized in path_set else None


def _parse_file(repo: Path, path: Path, text: str, module_index: dict[str, str]) -> dict[str, Any]:
    language = language_for(path)
    if path.suffix.lower() in MULTIMODAL_EXTS:
        return {"asset_type": path.suffix.lower().lstrip("."), "imports": [], "resolved_imports": [], "symbols": []}
    if language == "python":
        return _parse_python(repo, path, text, module_index)
    if language == "markdown":
        return _parse_markdown(text)
    if language == "toml":
        return _parse_toml(path, text)
    if language in {"json", "jsonl"}:
        return _parse_json_like(text)
    if language == "yaml":
        return _parse_yaml_like(text)
    if language in {"javascript", "typescript"}:
        return _parse_ecmascript(repo, path, text)
    if language in {"go", "rust", "java", "kotlin", "csharp"}:
        return _parse_go_rust_java_like(text, language)
    if language == "sql":
        return _parse_sql(text)
    if language in {"shell", "powershell"}:
        return _parse_shell_like(text, language)
    if language == "dockerfile":
        return _parse_dockerfile(text)
    return {"imports": [], "resolved_imports": [], "symbols": [], "terms": _extract_terms(text)}


def _parse_cache_key(relative: str, file_sha: str) -> str:
    return sha256_text(f"{PARSER_VERSION}\0{relative}\0{file_sha}")


def _load_parse_cache(cache_dir: Path | None, relative: str, file_sha: str) -> dict[str, Any] | None:
    if cache_dir is None:
        return None
    path = cache_dir / f"{_parse_cache_key(relative, file_sha)}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("parser_version") != PARSER_VERSION or payload.get("file_sha256") != file_sha:
        return None
    parsed = payload.get("parsed")
    return parsed if isinstance(parsed, dict) else None


def _write_parse_cache(cache_dir: Path | None, relative: str, file_sha: str, parsed: dict[str, Any]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "parser_version": PARSER_VERSION,
        "relative_path": relative,
        "file_sha256": file_sha,
        "parsed": parsed,
    }
    atomic_json(cache_dir / f"{_parse_cache_key(relative, file_sha)}.json", payload)


def _edge(source: str, target: str, kind: str, *, confidence: float = 1.0, provenance: str = "EXTRACTED", evidence: str | None = None, file: str | None = None, line: int | None = None, weight: float | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": source,
        "target": target,
        "kind": kind,
        "confidence": round(float(confidence), 3),
        "provenance": provenance,
    }
    if evidence:
        row["evidence"] = evidence[:260]
    if file:
        row["file"] = file
    if line:
        row["line"] = int(line)
    if weight is not None:
        row["weight"] = round(float(weight), 3)
    row["id"] = sha256_text(json.dumps({k: row[k] for k in sorted(row) if k != "id"}, sort_keys=True))[:20]
    return row


def _node(node_id: str, node_type: str, label: str, **meta: Any) -> dict[str, Any]:
    clean = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    return {"id": node_id, "type": node_type, "label": label, **clean}


def _calculate_centrality(file_rows: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    incoming: dict[str, int] = Counter()
    outgoing: dict[str, int] = Counter()
    weighted: dict[str, float] = Counter()
    for edge in edges:
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if not source.startswith("file:") and "::" in source:
            source_file = source.removeprefix("symbol:").split("::", 1)[0]
            source = f"file:{source_file}"
        if not target.startswith("file:") and "::" in target:
            target_file = target.removeprefix("symbol:").split("::", 1)[0]
            target = f"file:{target_file}"
        source_path = source.removeprefix("file:") if source.startswith("file:") else None
        target_path = target.removeprefix("file:") if target.startswith("file:") else None
        weight = float(edge.get("weight", 1.0) or 1.0)
        if source_path:
            outgoing[source_path] += 1
            weighted[source_path] += weight
        if target_path:
            incoming[target_path] += 1
            weighted[target_path] += weight
    for row in file_rows:
        path = str(row["path"])
        degree = incoming.get(path, 0) + outgoing.get(path, 0) + weighted.get(path, 0) * 0.2 + len(row.get("symbols", [])) * 0.16
        if row["kind"] in {"config", "script", "schema"}:
            degree += 0.8
        if row.get("entrypoint"):
            degree += 2.0
        row["incoming_file_imports"] = incoming.get(path, 0)
        row["outgoing_file_imports"] = outgoing.get(path, 0)
        row["centrality_hint"] = round(float(degree), 3)


def _file_graph_neighbors(file_rows: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, set[str]]:
    paths = {str(row.get("path")) for row in file_rows}
    neighbors: dict[str, set[str]] = {path: set() for path in paths}
    for edge in edges:
        kind = str(edge.get("kind", ""))
        if kind not in {"imports_file", "references_file", "tests", "configures", "calls_file", "same_package"}:
            continue
        source = str(edge.get("source", "")).removeprefix("file:")
        target = str(edge.get("target", "")).removeprefix("file:")
        if source in paths and target in paths and source != target:
            neighbors[source].add(target)
            neighbors[target].add(source)
    return neighbors


def _detect_communities(file_rows: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    neighbors = _file_graph_neighbors(file_rows, edges)
    # Add weak directory-level structure so sparse repos still cluster usefully.
    by_top: dict[str, list[str]] = {}
    for row in file_rows:
        path = str(row["path"])
        top = path.split("/", 1)[0]
        by_top.setdefault(top, []).append(path)
    for paths in by_top.values():
        if len(paths) <= 1 or len(paths) > 80:
            continue
        hub = paths[0]
        for path in paths[1:]:
            neighbors.setdefault(hub, set()).add(path)
            neighbors.setdefault(path, set()).add(hub)

    seen: set[str] = set()
    communities: list[list[str]] = []
    for path in sorted(neighbors):
        if path in seen:
            continue
        queue = deque([path])
        seen.add(path)
        component: list[str] = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for nxt in sorted(neighbors.get(current, set())):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        communities.append(sorted(component))

    rows_by_path = {str(row["path"]): row for row in file_rows}
    result: list[dict[str, Any]] = []
    for idx, paths in enumerate(sorted(communities, key=lambda group: (-len(group), group[0] if group else "")), 1):
        rows = [rows_by_path[path] for path in paths if path in rows_by_path]
        top_counts = Counter(path.split("/", 1)[0] for path in paths)
        lang_counts = Counter(str(row.get("language")) for row in rows)
        kind_counts = Counter(str(row.get("kind")) for row in rows)
        hottest = sorted(rows, key=lambda row: (-float(row.get("centrality_hint", 0.0) or 0.0), str(row.get("path"))))[:8]
        label = top_counts.most_common(1)[0][0] if top_counts else f"community-{idx}"
        community_id = f"C{idx:03d}"
        for row in rows:
            row["community_id"] = community_id
        result.append({
            "id": community_id,
            "label": label,
            "file_count": len(rows),
            "language_counts": dict(sorted(lang_counts.items())),
            "kind_counts": dict(sorted(kind_counts.items())),
            "hotspots": [str(row.get("path")) for row in hottest],
            "summary": f"{label}: {len(rows)} files; top kind {kind_counts.most_common(1)[0][0] if kind_counts else 'unknown'}; top language {lang_counts.most_common(1)[0][0] if lang_counts else 'unknown'}.",
        })
    return result


def _surprising_connections(edges: list[dict[str, Any]], file_rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    by_path = {str(row.get("path")): row for row in file_rows}
    surprises: list[dict[str, Any]] = []
    for edge in edges:
        if edge.get("kind") not in {"imports_file", "references_file", "tests", "calls_file"}:
            continue
        source = str(edge.get("source", "")).removeprefix("file:")
        target = str(edge.get("target", "")).removeprefix("file:")
        if source not in by_path or target not in by_path:
            continue
        srow = by_path[source]
        trow = by_path[target]
        if srow.get("community_id") != trow.get("community_id") or source.split("/", 1)[0] != target.split("/", 1)[0]:
            surprises.append({
                "source": source,
                "target": target,
                "kind": edge.get("kind"),
                "reason": "cross-community or cross-top-level relationship",
                "confidence": edge.get("confidence", 1.0),
            })
    return surprises[:limit]


def _knowledge_gaps(file_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    source_rows = [row for row in file_rows if row.get("kind") == "source"]
    test_count = sum(1 for row in file_rows if row.get("kind") == "test")
    if source_rows and not test_count:
        gaps.append({"kind": "missing_tests", "message": "Source files are indexed but no test files were found."})
    for row in sorted(source_rows, key=lambda r: -float(r.get("centrality_hint", 0.0) or 0.0))[:10]:
        if row.get("language") == "python" and not row.get("doc") and float(row.get("centrality_hint", 0.0) or 0.0) >= 2.0:
            gaps.append({"kind": "undocumented_hotspot", "path": row.get("path"), "message": "Central Python file has no module docstring."})
        if row.get("parse_error"):
            gaps.append({"kind": "parse_error", "path": row.get("path"), "message": str(row.get("parse_error"))})
    return gaps[:20]


def build_repo_graph(repo: Path, *, max_files: int = 1500, cache_dir: Path | None = None) -> dict[str, Any]:
    repo = repo.resolve()
    files = iter_repo_files(repo, max_files=max_files)
    module_index = _python_module_index(repo, files)
    path_set = {relpath(repo, path) for path in files}
    file_rows: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    language_counts: Counter[str] = Counter()
    top_level_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    entrypoints: dict[str, str] = {}
    cache_stats = {"enabled": cache_dir is not None, "hits": 0, "misses": 0}

    for path in files:
        relative = relpath(repo, path)
        language = language_for(path)
        kind = _file_kind(relative, language)
        language_counts[language] += 1
        kind_counts[kind] += 1
        top_level_counts[relative.split("/", 1)[0]] += 1
        try:
            raw_bytes = path.read_bytes() if path.suffix.lower() in MULTIMODAL_EXTS else b""
            stat = path.stat()
            size_bytes = int(stat.st_size)
        except OSError:
            raw_bytes = b""
            size_bytes = 0
        text = _safe_read(path)
        file_sha = sha256_bytes(raw_bytes) if raw_bytes else hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        parsed = _load_parse_cache(cache_dir, relative, file_sha)
        if parsed is None:
            cache_stats["misses"] += 1
            parsed = _parse_file(repo, path, text, module_index)
            _write_parse_cache(cache_dir, relative, file_sha, parsed)
        else:
            cache_stats["hits"] += 1
        if parsed.get("entrypoints"):
            entrypoints.update(parsed.get("entrypoints", {}))
        symbols = parsed.get("symbols", []) if isinstance(parsed.get("symbols"), list) else []
        imports = parsed.get("imports", []) if isinstance(parsed.get("imports"), list) else []
        resolved_imports = parsed.get("resolved_imports", []) if isinstance(parsed.get("resolved_imports"), list) else []
        path_refs = parsed.get("path_refs", []) if isinstance(parsed.get("path_refs"), list) else []
        calls = parsed.get("calls", []) if isinstance(parsed.get("calls"), list) else []
        row = {
            "path": relative,
            "node_id": f"file:{relative}",
            "language": language,
            "kind": kind,
            "size_bytes": size_bytes,
            "lines": _line_count(text),
            "sha256": file_sha,
            "symbols": symbols,
            "imports": imports,
            "resolved_imports": resolved_imports,
            "headings": parsed.get("headings", []),
            "path_refs": path_refs,
            "sections": parsed.get("sections", []),
            "links": parsed.get("links", []),
            "code_fences": parsed.get("code_fences", {}),
            "doc": parsed.get("doc", ""),
            "terms": parsed.get("terms", _extract_terms(text)),
            "parse_error": parsed.get("parse_error"),
            "asset_type": parsed.get("asset_type"),
        }
        file_rows.append(row)
        nodes.append(_node(f"file:{relative}", "file", relative, path=relative, language=language, kind=kind, size_bytes=size_bytes, lines=row["lines"]))
        for symbol in symbols:
            sid = f"symbol:{relative}::{symbol.get('qualname') or symbol.get('name')}"
            nodes.append(_node(sid, "symbol", str(symbol.get("qualname") or symbol.get("name")), path=relative, kind=symbol.get("kind"), line=symbol.get("line"), signature=symbol.get("signature")))
            edges.append(_edge(f"file:{relative}", sid, "contains", confidence=1.0, provenance="EXTRACTED", file=relative, line=symbol.get("line"), weight=0.6))
        for module in imports:
            nodes.append(_node(f"module:{module}", "module", str(module)))
            edges.append(_edge(f"file:{relative}", f"module:{module}", "imports", confidence=0.92, provenance="EXTRACTED", file=relative, evidence=str(module), weight=0.8))
        for target in resolved_imports:
            edges.append(_edge(f"file:{relative}", f"file:{target}", "imports_file", confidence=1.0, provenance="EXTRACTED", file=relative, evidence="resolved local import", weight=3.0))
        for ref in path_refs:
            target = _path_ref_to_file(repo, path, str(ref), path_set)
            if target and target != relative:
                edges.append(_edge(f"file:{relative}", f"file:{target}", "references_file", confidence=0.86, provenance="EXTRACTED", file=relative, evidence=str(ref), weight=1.5))
        for call in calls:
            caller = call.get("caller")
            callee = str(call.get("callee") or "")
            if not callee:
                continue
            source_id = f"symbol:{relative}::{caller}" if caller else f"file:{relative}"
            if call.get("resolved_symbol"):
                target_id = f"symbol:{relative}::{call.get('resolved_symbol')}"
                edges.append(_edge(source_id, target_id, "calls", confidence=0.84, provenance="EXTRACTED", file=relative, line=call.get("line"), evidence=callee, weight=1.2))
            else:
                target_id = f"call:{callee}"
                nodes.append(_node(target_id, "call", callee))
                edges.append(_edge(source_id, target_id, "calls_external", confidence=0.55, provenance="AMBIGUOUS", file=relative, line=call.get("line"), evidence=callee, weight=0.3))

    # Entrypoint links and likely test-source relationships.
    by_path = {str(row["path"]): row for row in file_rows}
    module_to_path = _python_module_index(repo, files)
    for name, target in entrypoints.items():
        module = str(target).split(":", 1)[0]
        target_path = _resolve_import(module, module_to_path)
        if target_path and target_path in by_path:
            by_path[target_path]["entrypoint"] = name
            edges.append(_edge("entrypoint:" + name, f"file:{target_path}", "entrypoint", confidence=1.0, provenance="EXTRACTED", evidence=str(target), weight=3.0))
            nodes.append(_node("entrypoint:" + name, "entrypoint", name, target=target))
    source_names = {Path(path).stem.replace("test_", "").replace("_test", ""): path for path, row in by_path.items() if row.get("kind") == "source"}
    for path, row in by_path.items():
        if row.get("kind") != "test":
            continue
        stem = Path(path).stem.replace("test_", "").replace("_test", "")
        target = source_names.get(stem)
        if target and target != path:
            edges.append(_edge(f"file:{path}", f"file:{target}", "tests", confidence=0.74, provenance="INFERRED", evidence="test/source filename similarity", weight=2.0))

    _calculate_centrality(file_rows, edges)
    communities = _detect_communities(file_rows, edges)
    for community in communities:
        nodes.append(_node(f"community:{community['id']}", "community", community["label"], file_count=community["file_count"], summary=community["summary"]))
        for path in community.get("hotspots", [])[:80]:
            edges.append(_edge(f"community:{community['id']}", f"file:{path}", "has_member", confidence=0.95, provenance="INFERRED", evidence="structural community membership", weight=0.5))

    hotspots = sorted(
        (
            {
                "path": row["path"], "kind": row["kind"], "language": row["language"],
                "community_id": row.get("community_id"),
                "centrality_hint": row["centrality_hint"],
                "incoming_file_imports": row["incoming_file_imports"],
                "outgoing_file_imports": row["outgoing_file_imports"],
                "symbols": [s.get("qualname") for s in row.get("symbols", [])[:16]],
            }
            for row in file_rows
        ),
        key=lambda row: (-float(row["centrality_hint"]), row["path"]),
    )[:40]

    # De-duplicate nodes/edges by id while preserving the first rich occurrence.
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_map.setdefault(str(node["id"]), node)
    edge_map: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_map.setdefault(str(edge["id"]), edge)
    edges = list(edge_map.values())[:60_000]
    nodes = list(node_map.values())[:80_000]

    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "repo_name": repo.name,
        "file_count": len(file_rows),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "language_counts": dict(sorted(language_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "top_level_counts": dict(sorted(top_level_counts.items())),
        "entrypoints": entrypoints,
        "files": file_rows,
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
        "hotspots": hotspots,
        "surprising_connections": _surprising_connections(edges, file_rows),
        "knowledge_gaps": _knowledge_gaps(file_rows),
        "cache": cache_stats,
        "limits": {"max_files": max_files, "truncated": len(files) >= max_files},
    }


def repo_fingerprint(repo: Path, *, max_files: int = 1500) -> dict[str, Any]:
    rows: list[str] = []
    files = iter_repo_files(repo, max_files=max_files)
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(f"{relpath(repo.resolve(), path.resolve())}\0{stat.st_size}\0{stat.st_mtime_ns}")
    digest = hashlib.sha256("\n".join(rows).encode("utf-8", errors="replace")).hexdigest()
    return {"sha256": digest, "file_count": len(rows), "max_files": max_files, "parser_version": PARSER_VERSION}


def quick_repo_facts(repo: Path, *, max_files: int = 1500) -> dict[str, Any]:
    files = iter_repo_files(repo, max_files=max_files)
    languages: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    multimodal = 0
    for path in files:
        language = language_for(path)
        languages[language] += 1
        relative = relpath(repo.resolve(), path.resolve())
        kinds[_file_kind(relative, language)] += 1
        if path.suffix.lower() in MULTIMODAL_EXTS:
            multimodal += 1
    return {
        "file_count": len(files),
        "language_counts": dict(sorted(languages.items())),
        "kind_counts": dict(sorted(kinds.items())),
        "multimodal_asset_count": multimodal,
        "truncated": len(files) >= max_files,
    }


def _tokens(text: str) -> set[str]:
    return {token for token in _word_tokens(text) if token not in STOPWORDS}


def _row_search_text(row: dict[str, Any]) -> str:
    symbols = " ".join(str(s.get("qualname") or s.get("name") or "") for s in row.get("symbols", []))
    headings = " ".join(str(h.get("title") or "") for h in row.get("headings", []))
    imports = " ".join(str(x) for x in row.get("imports", []))
    sections = " ".join(str(x) for x in row.get("sections", []))
    terms = " ".join(str(x) for x in row.get("terms", []))
    return " ".join([str(row.get("path", "")), str(row.get("kind", "")), str(row.get("language", "")), symbols, headings, imports, sections, terms, str(row.get("doc", ""))])


def task_context_selection(graph: dict[str, Any], task: str, *, max_files: int = 12) -> dict[str, Any]:
    terms = _tokens(task)
    files = graph.get("files", []) if isinstance(graph.get("files"), list) else []
    by_path = {str(row.get("path")): row for row in files if isinstance(row, dict)}
    incoming_neighbors: dict[str, set[str]] = {}
    outgoing_neighbors: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("kind") not in {"imports_file", "references_file", "tests", "entrypoint"}:
            continue
        source = str(edge.get("source", "")).removeprefix("file:")
        target = str(edge.get("target", "")).removeprefix("file:")
        if source.startswith("entrypoint:"):
            source = ""
        if source in by_path and target in by_path:
            outgoing_neighbors.setdefault(source, set()).add(target)
            incoming_neighbors.setdefault(target, set()).add(source)
    scored: list[dict[str, Any]] = []
    hotspot_paths = {hot.get("path") for hot in graph.get("hotspots", [])[:12] if isinstance(hot, dict)}
    for row in files:
        if not isinstance(row, dict):
            continue
        haystack = _tokens(_row_search_text(row))
        overlap = sorted(terms & haystack)
        path = str(row.get("path", ""))
        score = len(overlap) * 2.0 + float(row.get("centrality_hint", 0.0) or 0.0) * 0.18
        why: list[str] = []
        if overlap:
            why.append("matched task terms: " + ", ".join(overlap[:8]))
        task_lower = task.lower()
        if "test" in task_lower and row.get("kind") == "test":
            score += 2.5
            why.append("test file for requested validation")
        if any(word in task_lower for word in ("config", "install", "package", "cli", "command", "script")) and row.get("kind") in {"config", "script"}:
            score += 1.5
            why.append("configuration/script likely relevant")
        if any(word in task_lower for word in ("readme", "docs", "document", "roadmap", "report")) and row.get("kind") == "docs":
            score += 1.5
            why.append("documentation likely relevant")
        if any(word in task_lower for word in ("database", "schema", "sql", "migration")) and row.get("kind") == "schema":
            score += 1.8
            why.append("schema/data path likely relevant")
        if path in hotspot_paths:
            score += 0.4
            why.append("central repo hotspot")
        if score > 0:
            scored.append({"path": path, "score": round(score, 3), "why": why or ["centrality/neighbor signal"], "community_id": row.get("community_id")})
    selected = sorted(scored, key=lambda row: (-float(row["score"]), row["path"]))[:max_files]
    selected_paths = {row["path"] for row in selected}
    neighbors: list[dict[str, Any]] = []
    for path in list(selected_paths):
        for target in sorted(outgoing_neighbors.get(path, set()) | incoming_neighbors.get(path, set())):
            if target not in selected_paths and target in by_path:
                neighbors.append({"path": target, "near": path, "kind": by_path[target].get("kind"), "language": by_path[target].get("language"), "community_id": by_path[target].get("community_id")})
    return {
        "task_terms": sorted(terms)[:80],
        "selected": selected,
        "neighbors": neighbors[:max(0, max_files - len(selected))],
        "communities": sorted({str(item.get("community_id")) for item in selected if item.get("community_id")}),
    }


def query_graph(graph: dict[str, Any], query: str, *, budget: int = 20) -> list[dict[str, Any]]:
    terms = _tokens(query)
    results: list[dict[str, Any]] = []
    for row in graph.get("files", []) if isinstance(graph.get("files"), list) else []:
        if not isinstance(row, dict):
            continue
        haystack = _tokens(_row_search_text(row))
        overlap = sorted(terms & haystack)
        if not overlap:
            continue
        score = len(overlap) * 2.0 + math.log1p(float(row.get("centrality_hint", 0.0) or 0.0))
        results.append({
            "type": "file",
            "id": f"file:{row.get('path')}",
            "path": row.get("path"),
            "score": round(score, 3),
            "matches": overlap[:10],
            "kind": row.get("kind"),
            "language": row.get("language"),
            "community_id": row.get("community_id"),
            "centrality_hint": row.get("centrality_hint"),
        })
        for symbol in row.get("symbols", [])[:120]:
            hay = _tokens(" ".join(str(symbol.get(k, "")) for k in ("name", "qualname", "kind", "doc", "signature")))
            sym_overlap = sorted(terms & hay)
            if sym_overlap:
                results.append({
                    "type": "symbol",
                    "id": f"symbol:{row.get('path')}::{symbol.get('qualname') or symbol.get('name')}",
                    "path": row.get("path"),
                    "symbol": symbol.get("qualname") or symbol.get("name"),
                    "line": symbol.get("line"),
                    "score": round(score + len(sym_overlap) * 1.5, 3),
                    "matches": sym_overlap[:10],
                    "kind": symbol.get("kind"),
                    "community_id": row.get("community_id"),
                })
    return sorted(results, key=lambda row: (-float(row.get("score", 0.0)), str(row.get("id"))))[:budget]


def path_between(graph: dict[str, Any], source_query: str, target_query: str, *, max_depth: int = 5, budget: int = 8) -> list[list[dict[str, Any]]]:
    source_hits = query_graph(graph, source_query, budget=budget)
    target_hits = query_graph(graph, target_query, budget=budget)
    source_ids = [str(hit.get("id")) for hit in source_hits]
    target_ids = {str(hit.get("id")) for hit in target_hits}
    if not source_ids or not target_ids:
        return []
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for edge in graph.get("edges", []) if isinstance(graph.get("edges"), list) else []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        adjacency.setdefault(source, []).append((target, edge))
        adjacency.setdefault(target, []).append((source, edge))
    paths: list[list[dict[str, Any]]] = []
    for source in source_ids:
        queue = deque([(source, [])])
        seen = {source}
        while queue and len(paths) < budget:
            node_id, route = queue.popleft()
            if len(route) > max_depth:
                continue
            if node_id in target_ids and route:
                paths.append(route)
                continue
            for nxt, edge in adjacency.get(node_id, [])[:80]:
                if nxt in seen:
                    continue
                seen.add(nxt)
                step = {"from": node_id, "to": nxt, "kind": edge.get("kind"), "confidence": edge.get("confidence"), "provenance": edge.get("provenance"), "evidence": edge.get("evidence")}
                queue.append((nxt, route + [step]))
    return paths[:budget]


def render_repo_report(graph: dict[str, Any]) -> str:
    lines = [
        "# Repository graph report",
        "",
        "This is a local supervisor-generated knowledge graph. Use it to navigate by structure before opening broad file sets.",
        "",
        f"- Repository: `{graph.get('repo_name', '')}`",
        f"- Indexed files: `{graph.get('file_count', 0)}`",
        f"- Nodes / edges: `{graph.get('node_count', 0)}` / `{graph.get('edge_count', 0)}`",
        f"- Generated: `{graph.get('generated_at', '')}`",
        f"- Languages: `{graph.get('language_counts', {})}`",
        f"- File kinds: `{graph.get('kind_counts', {})}`",
        f"- Top-level groups: `{graph.get('top_level_counts', {})}`",
        f"- Cache: `{graph.get('cache', {})}`",
    ]
    entrypoints = graph.get("entrypoints") if isinstance(graph.get("entrypoints"), dict) else {}
    if entrypoints:
        lines.extend(["", "## Entrypoints", ""])
        for name, target in sorted(entrypoints.items()):
            lines.append(f"- `{name}` -> `{target}`")
    lines.extend(["", "## Communities", ""])
    communities = graph.get("communities", []) if isinstance(graph.get("communities"), list) else []
    if not communities:
        lines.append("No communities identified yet.")
    for row in communities[:20]:
        lines.append(f"- `{row.get('id')}` **{row.get('label')}** — {row.get('summary')} Hotspots: {', '.join('`' + str(x) + '`' for x in row.get('hotspots', [])[:5])}")
    lines.extend(["", "## God nodes / navigation hotspots", ""])
    hotspots = graph.get("hotspots", []) if isinstance(graph.get("hotspots"), list) else []
    if not hotspots:
        lines.append("No hotspots identified yet.")
    for row in hotspots[:20]:
        lines.append(
            f"- `{row.get('path')}` ({row.get('kind')}/{row.get('language')}, {row.get('community_id')}): "
            f"centrality `{row.get('centrality_hint')}`, incoming `{row.get('incoming_file_imports')}`, "
            f"outgoing `{row.get('outgoing_file_imports')}`; symbols `{', '.join(str(x) for x in row.get('symbols', [])[:8])}`"
        )
    surprises = graph.get("surprising_connections", []) if isinstance(graph.get("surprising_connections"), list) else []
    if surprises:
        lines.extend(["", "## Surprising cross-connections", ""])
        for row in surprises[:12]:
            lines.append(f"- `{row.get('source')}` --{row.get('kind')}--> `{row.get('target')}` ({row.get('reason')})")
    gaps = graph.get("knowledge_gaps", []) if isinstance(graph.get("knowledge_gaps"), list) else []
    if gaps:
        lines.extend(["", "## Knowledge gaps", ""])
        for row in gaps[:12]:
            path = f" `{row.get('path')}`" if row.get("path") else ""
            lines.append(f"- `{row.get('kind')}`{path}: {row.get('message')}")
    lines.extend([
        "",
        "## Suggested graph-first questions",
        "",
        "- Which community owns the requested behavior?",
        "- What are the god nodes and bridge files before editing?",
        "- What path connects the requested feature to tests/configuration?",
        "- What changed since the last cache fingerprint?",
        "",
        "## How workers should use this",
        "",
        "1. Read `repo-context-pack.md` first for task-specific file candidates.",
        "2. Use `repo-map/graph.json`, `repo-map/graph.html`, or `hermes-legion-commander repo-graph query` before broad recursive reads.",
        "3. Open selected files, their community hotspots, and immediate neighbors before scanning the whole repository.",
        "4. Cite exact paths from this map in handoffs, then validate with tests and diffs.",
        "5. Treat INFERRED and AMBIGUOUS edges as navigation hints, not proof of correctness.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_context_pack(graph: dict[str, Any], task: str, selection: dict[str, Any]) -> str:
    by_path = {str(row.get("path")): row for row in graph.get("files", []) if isinstance(row, dict)}
    community_map = {str(row.get("id")): row for row in graph.get("communities", []) if isinstance(row, dict)}
    lines = [
        "# Task-specific repository context pack",
        "",
        "This pack was generated from the current stage task and the local repo graph to reduce prompt and navigation cost.",
        "",
        f"- Task terms: `{', '.join(selection.get('task_terms', [])[:50])}`",
        f"- Candidate files selected: `{len(selection.get('selected', []))}`",
        f"- Communities selected: `{', '.join(selection.get('communities', [])) or 'none'}`",
        "",
        "## Start here",
        "",
    ]
    selected = selection.get("selected", []) if isinstance(selection.get("selected"), list) else []
    if not selected:
        lines.append("No strongly matching files were found. Use `repo-map/REPO_MAP.md` hotspots and repository search.")
    for item in selected:
        row = by_path.get(str(item.get("path")), {})
        symbols = ", ".join(str(s.get("qualname") or s.get("name") or "") for s in row.get("symbols", [])[:10])
        headings = ", ".join(str(h.get("title") or "") for h in row.get("headings", [])[:6])
        details = symbols or headings or ", ".join(str(x) for x in row.get("sections", [])[:8]) or str(row.get("doc", ""))[:180]
        why = "; ".join(str(x) for x in item.get("why", []))
        lines.append(
            f"- `{item.get('path')}` score `{item.get('score')}` — {why}. "
            f"Kind `{row.get('kind')}`, language `{row.get('language')}`, community `{row.get('community_id')}`, lines `{row.get('lines')}`. "
            f"Key items: {details or 'none captured'}."
        )
    neighbors = selection.get("neighbors", []) if isinstance(selection.get("neighbors"), list) else []
    if neighbors:
        lines.extend(["", "## Immediate graph neighbors", ""])
        for item in neighbors[:12]:
            lines.append(f"- `{item.get('path')}` near `{item.get('near')}` ({item.get('kind')}/{item.get('language')}, {item.get('community_id')})")
    if selection.get("communities"):
        lines.extend(["", "## Relevant communities", ""])
        for cid in selection.get("communities", [])[:8]:
            community = community_map.get(str(cid), {})
            lines.append(f"- `{cid}` {community.get('label', '')}: {community.get('summary', '')}")
    lines.extend([
        "",
        "## Context discipline",
        "",
        "- Prefer these files before broad recursive reads.",
        "- Add missing files only when imports, tests, path edges, or errors prove they are relevant.",
        "- Use `repo-map/graph.json` for structured lookup; do not paste the entire graph into prompts.",
        "- In handoffs, mark whether evidence came from EXTRACTED, INFERRED, or AMBIGUOUS edges.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_graph_html(graph: dict[str, Any], *, max_nodes: int = 500, max_edges: int = 1200) -> str:
    nodes = graph.get("nodes", [])[:max_nodes] if isinstance(graph.get("nodes"), list) else []
    visible_ids = {str(node.get("id")) for node in nodes if isinstance(node, dict)}
    edges = [edge for edge in (graph.get("edges", []) if isinstance(graph.get("edges"), list) else []) if str(edge.get("source")) in visible_ids and str(edge.get("target")) in visible_ids][:max_edges]
    payload = json.dumps({"nodes": nodes, "edges": edges, "communities": graph.get("communities", []), "hotspots": graph.get("hotspots", [])}, ensure_ascii=False)
    title = html.escape(str(graph.get("repo_name", "Repository graph")))
    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{title} graph</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #111827; color: #f9fafb; }}
header {{ padding: 16px 20px; border-bottom: 1px solid #374151; background: #0f172a; }}
main {{ display: grid; grid-template-columns: 320px 1fr; min-height: calc(100vh - 70px); }}
aside {{ border-right: 1px solid #374151; padding: 14px; overflow: auto; }}
section {{ padding: 14px; overflow: auto; }}
input, select {{ width: 100%; box-sizing: border-box; margin: 6px 0 12px; padding: 8px; border-radius: 8px; border: 1px solid #4b5563; background: #111827; color: #f9fafb; }}
.node {{ padding: 8px; margin: 6px 0; border: 1px solid #374151; border-radius: 8px; cursor: pointer; background: #1f2937; }}
.node:hover {{ border-color: #93c5fd; }}
.badge {{ font-size: 11px; padding: 2px 6px; border-radius: 999px; background: #374151; margin-left: 6px; }}
#detail {{ white-space: pre-wrap; background: #0b1220; border: 1px solid #374151; border-radius: 10px; padding: 14px; }}
.edge {{ border-bottom: 1px solid #273449; padding: 6px 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
@media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; }} aside {{ border-right: none; border-bottom: 1px solid #374151; }} }}
</style>
</head>
<body>
<header><strong>{title}</strong> — local knowledge graph ({len(nodes)} visible nodes, {len(edges)} visible edges)</header>
<main>
<aside>
<label>Search</label><input id=\"search\" placeholder=\"path, symbol, community...\">
<label>Type</label><select id=\"type\"><option value=\"\">all</option></select>
<div id=\"nodes\"></div>
</aside>
<section>
<div class=\"grid\"><div><h2>Selected node</h2><div id=\"detail\">Select a node.</div></div><div><h2>Neighbors</h2><div id=\"neighbors\"></div></div></div>
<h2>Hotspots</h2><div id=\"hotspots\"></div>
</section>
</main>
<script id=\"graph-data\" type=\"application/json\">{html.escape(payload)}</script>
<script>
const graph = JSON.parse(document.getElementById('graph-data').textContent);
const nodeMap = new Map(graph.nodes.map(n => [n.id, n]));
const typeSel = document.getElementById('type');
[...new Set(graph.nodes.map(n => n.type).filter(Boolean))].sort().forEach(t => {{ const o=document.createElement('option'); o.value=t; o.textContent=t; typeSel.appendChild(o); }});
function renderNodes() {{
  const q = document.getElementById('search').value.toLowerCase();
  const t = typeSel.value;
  const box = document.getElementById('nodes'); box.innerHTML='';
  graph.nodes.filter(n => (!t || n.type===t) && JSON.stringify(n).toLowerCase().includes(q)).slice(0, 220).forEach(n => {{
    const div = document.createElement('div'); div.className='node'; div.innerHTML = `<strong>${{n.label || n.id}}</strong><span class=\"badge\">${{n.type}}</span><br><small>${{n.path || n.id}}</small>`; div.onclick=()=>selectNode(n.id); box.appendChild(div);
  }});
}}
function selectNode(id) {{
  const n = nodeMap.get(id); document.getElementById('detail').textContent = JSON.stringify(n, null, 2);
  const neigh = graph.edges.filter(e => e.source===id || e.target===id).slice(0, 120);
  const box = document.getElementById('neighbors'); box.innerHTML='';
  neigh.forEach(e => {{ const other = e.source===id ? e.target : e.source; const div=document.createElement('div'); div.className='edge'; div.textContent = `${{e.kind}}: ${{other}} (${{e.provenance || ''}}, conf=${{e.confidence || ''}})`; box.appendChild(div); }});
}}
document.getElementById('search').oninput=renderNodes; typeSel.onchange=renderNodes;
document.getElementById('hotspots').innerHTML = (graph.hotspots || []).slice(0,25).map(h => `<div class=\"edge\"><strong>${{h.path}}</strong> centrality ${{h.centrality_hint}} — ${{h.kind}}/${{h.language}} ${{h.community_id || ''}}</div>`).join('');
renderNodes();
</script>
</body>
</html>
"""


def render_wiki_pages(repo_map_dir: Path, graph: dict[str, Any]) -> None:
    wiki_dir = repo_map_dir / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for community in graph.get("communities", []) if isinstance(graph.get("communities"), list) else []:
        cid = str(community.get("id"))
        lines = [f"# {cid} {community.get('label', '')}", "", str(community.get("summary", "")), "", "## Hotspots", ""]
        for path in community.get("hotspots", [])[:40]:
            lines.append(f"- [[{path.replace('/', '__')}]]")
        atomic_write(wiki_dir / f"{cid}.md", "\n".join(lines).rstrip() + "\n")
    for row in graph.get("files", []) if isinstance(graph.get("files"), list) else []:
        path = str(row.get("path"))
        filename = path.replace("/", "__") + ".md"
        lines = [f"# {path}", "", f"- Kind: `{row.get('kind')}`", f"- Language: `{row.get('language')}`", f"- Community: `{row.get('community_id')}`", f"- Centrality: `{row.get('centrality_hint')}`", "", "## Symbols", ""]
        for sym in row.get("symbols", [])[:80]:
            lines.append(f"- `{sym.get('qualname') or sym.get('name')}` line `{sym.get('line')}` {sym.get('kind', '')}")
        if row.get("headings"):
            lines.extend(["", "## Headings", ""])
            for heading in row.get("headings", [])[:80]:
                lines.append(f"- {'#' * int(heading.get('level', 1))} {heading.get('title')} (line {heading.get('line')})")
        atomic_write(wiki_dir / filename, "\n".join(lines).rstrip() + "\n")


def refresh_repo_intelligence(context_dir: Path, repo: Path, *, task_prompt: str | None = None, max_files: int = 1500) -> dict[str, Any]:
    """Build or refresh the repo graph and optional task-specific context pack."""
    repo = repo.resolve()
    repo_map_dir = context_dir / "repo-map"
    repo_map_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = repo_fingerprint(repo, max_files=max_files)
    meta_path = repo_map_dir / "meta.json"
    graph_path = repo_map_dir / "graph.json"
    graph: dict[str, Any] | None = None
    prior_meta: dict[str, Any] = {}
    if meta_path.is_file() and graph_path.is_file():
        try:
            prior_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if prior_meta.get("fingerprint") == fingerprint and prior_meta.get("schema_version") == GRAPH_SCHEMA_VERSION:
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            graph = None
    if graph is None:
        graph = build_repo_graph(repo, max_files=max_files, cache_dir=repo_map_dir / "cache")
        atomic_json(graph_path, graph)
        index_lines = [json.dumps(row, sort_keys=True) for row in graph.get("files", [])]
        atomic_write(repo_map_dir / "repo-map-index.jsonl", "\n".join(index_lines) + ("\n" if index_lines else ""))
        atomic_write(repo_map_dir / "REPO_MAP.md", render_repo_report(graph))
        atomic_write(repo_map_dir / "graph.html", render_graph_html(graph))
        render_wiki_pages(repo_map_dir, graph)
        atomic_json(meta_path, {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "updated_at": dt.datetime.now(UTC).isoformat(),
            "fingerprint": fingerprint,
            "source": "local static repository knowledge graph",
            "artifacts": ["graph.json", "graph.html", "GRAPH_REPORT.md", "REPO_MAP.md", "repo-map-index.jsonl", "wiki/"],
        })
        atomic_write(repo_map_dir / "GRAPH_REPORT.md", render_repo_report(graph))
    if task_prompt is not None:
        selection = task_context_selection(graph, task_prompt)
        atomic_json(context_dir / "repo-context-pack.json", selection)
        atomic_write(context_dir / "repo-context-pack.md", render_context_pack(graph, task_prompt, selection))
    return graph


def _load_graph(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _print_query_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No graph matches found.\n"
    lines: list[str] = []
    for row in results:
        if row.get("type") == "symbol":
            lines.append(f"- symbol `{row.get('symbol')}` in `{row.get('path')}` line `{row.get('line')}` score `{row.get('score')}` matches `{', '.join(row.get('matches', []))}`")
        else:
            lines.append(f"- file `{row.get('path')}` ({row.get('kind')}/{row.get('language')}, {row.get('community_id')}) score `{row.get('score')}` matches `{', '.join(row.get('matches', []))}`")
    return "\n".join(lines) + "\n"


def cli_main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="hermes-legion-commander repo-graph", description="Build and query the local repository knowledge graph.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build graph artifacts for a repository")
    build.add_argument("repo", nargs="?", default=".")
    build.add_argument("--out", default="shared-context/repo-map", help="Output directory for graph artifacts")
    build.add_argument("--max-files", type=int, default=1500)
    build.add_argument("--task", default=None, help="Optional task prompt for repo-context-pack generation")
    query = sub.add_parser("query", help="Query a graph.json file")
    query.add_argument("text")
    query.add_argument("--graph", default="shared-context/repo-map/graph.json")
    query.add_argument("--budget", type=int, default=20)
    path_cmd = sub.add_parser("path", help="Find structural paths between two graph queries")
    path_cmd.add_argument("source")
    path_cmd.add_argument("target")
    path_cmd.add_argument("--graph", default="shared-context/repo-map/graph.json")
    path_cmd.add_argument("--max-depth", type=int, default=5)
    path_cmd.add_argument("--budget", type=int, default=8)
    args = parser.parse_args(argv)
    if args.command == "build":
        repo = Path(args.repo).resolve()
        out = Path(args.out).resolve()
        # If --out is a repo-map directory, context is its parent; otherwise use out itself as context.
        context_dir = out.parent if out.name == "repo-map" else out
        graph = refresh_repo_intelligence(context_dir, repo, task_prompt=args.task, max_files=args.max_files)
        print(f"Built repo graph: {context_dir / 'repo-map' / 'graph.json'} ({graph.get('file_count')} files, {graph.get('node_count')} nodes, {graph.get('edge_count')} edges)")
        return 0
    if args.command == "query":
        graph = _load_graph(Path(args.graph))
        print(_print_query_results(query_graph(graph, args.text, budget=args.budget)), end="")
        return 0
    if args.command == "path":
        graph = _load_graph(Path(args.graph))
        paths = path_between(graph, args.source, args.target, max_depth=args.max_depth, budget=args.budget)
        if not paths:
            print("No structural path found.")
            return 1
        for idx, route in enumerate(paths, 1):
            print(f"Path {idx}:")
            for step in route:
                print(f"  {step['from']} --{step['kind']}--> {step['to']} ({step.get('provenance')}, conf={step.get('confidence')})")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main())
