"""
MCP adapter for ContextCore unified search backend.

LLM Tool Routing Guide (applies to Claude, Cursor, Cline, OpenCode, etc.)
--------------------------------------------------------------------------
Use this order unless the user explicitly asks otherwise:

1) `search`
   - First tool for user questions about their files/content.
   - Use modality="all" unless user explicitly restricts to text/image/video/audio.
   - If empty or weak results: then call `index_content` and retry `search`.

2) `fetch_content`
   - Use after `search` when you need deeper details from a specific file.
   - For videos this returns frame descriptions + transcript excerpts.
   - For images this returns OCR text (if available) + file metadata.

3) `get_neighbors`
   - Use for adjacent context around a specific text/audio chunk.

4) `list_sources`
   - Use when user asks what is indexed, what folders are watched, or index counts.

5) `index_content`
   - Use only when content is missing/stale or user asks to reindex.
   - Do not call repeatedly for every query.

6) `prepare_file_for_tool` / `reveal_file`
   - Use when user wants to open, attach, or inspect local files in GUI tools.

7) Codebase tools (`search_code_chunks`, `get_codebase_context`,
   `get_codebase_index`, `get_module_detail`, `get_file_content`)
   - Use for repository reasoning tasks.
   - Prefer `search_code_chunks` for precise, minimal snippets.
   - Use `get_codebase_context` for broad repository orientation.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from cli.constants import DEFAULT_BACKEND_URL

SERVER_NAME = "contextcore-unified"
DEFAULT_TIMEOUT_SECONDS = 120

BACKEND_BASE_URL = os.getenv("CONTEXTCORE_API_BASE_URL", DEFAULT_BACKEND_URL).rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("CONTEXTCORE_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
PROJECT_ROOT = Path(__file__).resolve().parent
RETRIEVAL_BUDGET_MAX_CALLS = int(os.getenv("CONTEXTCORE_RETRIEVAL_BUDGET", "4"))

# Import config for storage path
sys.path.insert(0, str(PROJECT_ROOT))
from config import get_storage_dir
_STORAGE_DIR = get_storage_dir()

mcp = FastMCP(SERVER_NAME, json_response=True)
_BUDGET_LOCK = threading.Lock()
_SESSION_BUDGETS: dict[str, int] = {}
_SESSION_LAST_QUERY: dict[str, str] = {}
_FEEDBACK_DB = _STORAGE_DIR / "storage" / "mcp_feedback.db"

LOCAL_FILESYSTEM_TOOLS = {
    "claude-code",
    "cline",
    "aider",
    "opencode",
    "goose",
    "continue",
    "cursor",
    "windsurf",
    "codex",
}
REMOTE_ONLY_TOOLS = {
    "claude-desktop",
    "claude.ai",
    "chatgpt-web",
    "gemini-web",
    "perplexity",
    "browser-chat",
}

VCS_MARKERS = (".git", ".hg", ".svn")
LANGUAGE_MANIFEST_MARKERS: dict[str, tuple[str, ...]] = {
    "node": ("package.json",),
    "python": ("pyproject.toml", "setup.py", "setup.cfg"),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "ruby": ("Gemfile",),
    "php": ("composer.json",),
    "elixir": ("mix.exs",),
    "dart": ("pubspec.yaml",),
    "dotnet": (".csproj", ".sln"),
    "c_cpp": ("CMakeLists.txt", "Makefile"),
}
FRAMEWORK_MARKERS = (
    "next.config.js",
    "vite.config.js",
    "webpack.config.js",
    "tsconfig.json",
    "angular.json",
    "vue.config.js",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    "jest.config.js",
    "pytest.ini",
    "phpunit.xml",
)
COMMON_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "out",
    "target",
    "coverage",
}
EXCLUDES_BY_PROJECT_TYPE: dict[str, set[str]] = {
    "node": {"node_modules", ".next", "dist", "build", "coverage"},
    "python": {".venv", "venv", "__pycache__", ".eggs", "build", "dist", ".tox"},
    "rust": {"target"},
    "go": {"vendor", "bin"},
    "java": {"target", ".gradle", "build"},
    "ruby": {"vendor", ".bundle"},
    "php": {"vendor"},
    "dotnet": {"bin", "obj"},
    "c_cpp": {"build", "out"},
}


def _request_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    url = f"{BACKEND_BASE_URL}{path}"
    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT if timeout is None else timeout,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": "backend_unreachable",
            "message": str(exc),
            "backend_url": BACKEND_BASE_URL,
            "path": path,
        }

    if not resp.ok:
        try:
            detail: Any = resp.json()
        except ValueError:
            detail = resp.text
        return {
            "ok": False,
            "error": "backend_error",
            "status_code": resp.status_code,
            "detail": detail,
            "path": path,
        }

    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}
    return {"ok": True, "data": payload}


def _safe_sql_count(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> int:
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return int(row[0] if row and row[0] is not None else 0)
    except Exception:
        return 0


def _init_feedback_db() -> None:
    _FEEDBACK_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_FEEDBACK_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refine_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            session_id TEXT,
            original_query TEXT,
            reason TEXT,
            refined_query TEXT,
            exclude_sources TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _log_refine_feedback(
    session_id: str,
    original_query: str,
    reason: str,
    refined_query: str,
    exclude_sources: list[str] | None,
) -> None:
    try:
        _init_feedback_db()
        conn = sqlite3.connect(str(_FEEDBACK_DB))
        conn.execute(
            """
            INSERT INTO refine_feedback(session_id, original_query, reason, refined_query, exclude_sources)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, original_query, reason, refined_query, ",".join(exclude_sources or [])),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _consume_budget(session_id: str, reset: bool = False) -> dict[str, Any]:
    sid = session_id.strip() or "default"
    with _BUDGET_LOCK:
        if reset or sid not in _SESSION_BUDGETS:
            _SESSION_BUDGETS[sid] = RETRIEVAL_BUDGET_MAX_CALLS
        if _SESSION_BUDGETS[sid] <= 0:
            return {
                "ok": False,
                "error": "retrieval_budget_exhausted",
                "session_id": sid,
                "budget_remaining": 0,
                "budget_max": RETRIEVAL_BUDGET_MAX_CALLS,
            }
        _SESSION_BUDGETS[sid] -= 1
        return {
            "ok": True,
            "session_id": sid,
            "budget_remaining": _SESSION_BUDGETS[sid],
            "budget_max": RETRIEVAL_BUDGET_MAX_CALLS,
        }


def _reveal_file_in_explorer(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "file_not_found", "path": str(path)}

    try:
        if sys.platform.startswith("win"):
            target = str(path.resolve())
            # Explorer select mode. Quoted form is more reliable for spaces/special chars.
            try:
                subprocess.Popen(["explorer.exe", f'/select,"{target}"'])
            except Exception:
                # Fallback form used by some shells/setups.
                subprocess.Popen(["explorer.exe", f"/select,{target}"])
            return {
                "ok": True,
                "opened": "explorer",
                "path": target,
                "note": "Requested highlighted selection in Explorer",
            }
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return {"ok": True, "opened": "finder", "path": str(path)}

        # Linux fallback: open containing directory.
        subprocess.Popen(["xdg-open", str(path.parent)])
        return {"ok": True, "opened": "file-manager", "path": str(path), "note": "Opened parent directory"}
    except Exception as exc:
        return {"ok": False, "error": "reveal_failed", "path": str(path), "message": str(exc)}


def _normalize_tool_name(tool: str) -> str:
    return (tool or "").strip().lower()


def _should_auto_reset_budget(session_id: str, query: str) -> bool:
    sid = session_id.strip() or "default"
    normalized = " ".join((query or "").strip().lower().split())
    with _BUDGET_LOCK:
        prev = _SESSION_LAST_QUERY.get(sid)
        _SESSION_LAST_QUERY[sid] = normalized
    return prev != normalized


def _load_source_config() -> dict[str, Any]:
    # Deprecated: prefer list_sources() logic directly
    return {}


def _manifest_markers_at(path: Path) -> list[str]:
    markers: list[str] = []
    for marker in (
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "mix.exs",
        "pubspec.yaml",
        "CMakeLists.txt",
        "Makefile",
    ):
        if (path / marker).exists():
            markers.append(marker)

    for ext_marker in (".csproj", ".sln"):
        if list(path.glob(f"*{ext_marker}")):
            markers.append(ext_marker)
    return sorted(set(markers))


def _framework_markers_at(path: Path) -> list[str]:
    found: list[str] = []
    for marker in FRAMEWORK_MARKERS:
        if (path / marker).exists():
            found.append(marker)
    return sorted(found)


def _classify_project_types(root: Path) -> list[str]:
    types: list[str] = []
    for project_type, markers in LANGUAGE_MANIFEST_MARKERS.items():
        for marker in markers:
            if marker.startswith("."):
                if list(root.glob(f"*{marker}")):
                    types.append(project_type)
                    break
            elif (root / marker).exists():
                types.append(project_type)
                break
    return sorted(set(types))


def _find_project_root(start_path: Path) -> tuple[Path, dict[str, Any]]:
    current = start_path if start_path.is_dir() else start_path.parent
    chain = [current, *list(current.parents)]

    for p in chain:
        found_vcs = [m for m in VCS_MARKERS if (p / m).exists()]
        if found_vcs:
            return p, {"method": "vcs", "vcs_markers": found_vcs}

    for p in chain:
        manifests = _manifest_markers_at(p)
        if manifests:
            return p, {"method": "manifest", "manifest_markers": manifests}

    for p in chain:
        fw = _framework_markers_at(p)
        if fw:
            return p, {"method": "framework", "framework_markers": fw}

    return current, {"method": "fallback"}


def _classify_name_style(stem: str) -> str:
    if not stem:
        return "other"
    if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", stem):
        return "snake"
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", stem):
        return "kebab"
    if re.fullmatch(r"[a-z]+([A-Z][a-z0-9]*)+", stem):
        return "camel"
    return "other"


def _scan_code_signals(root: Path, max_scan_files: int) -> dict[str, Any]:
    file_count = 0
    test_file_count = 0
    import_link_count = 0
    readme_present = any((root / n).exists() for n in ("README", "README.md", "README.txt"))
    changelog_present = any((root / n).exists() for n in ("CHANGELOG", "CHANGELOG.md", "HISTORY.md"))
    generated_or_dep_dirs: set[str] = set()
    name_style_counts = {"snake": 0, "kebab": 0, "camel": 0, "other": 0}
    base_names: set[str] = set()
    code_files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        if file_count >= max_scan_files:
            break
        dirnames[:] = [d for d in dirnames if d not in COMMON_EXCLUDE_DIRS]
        generated_or_dep_dirs.update(d for d in dirnames if d in COMMON_EXCLUDE_DIRS)

        for fname in filenames:
            if file_count >= max_scan_files:
                break
            file_count += 1
            lower_name = fname.lower()
            stem = Path(fname).stem
            name_style_counts[_classify_name_style(stem)] += 1
            base_names.add(stem.lower())

            if (
                lower_name.startswith("test_")
                or lower_name.endswith("_test.py")
                or ".test." in lower_name
                or ".spec." in lower_name
            ):
                test_file_count += 1

            ext = Path(fname).suffix.lower()
            if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".php", ".rb"}:
                code_files.append(Path(dirpath) / fname)

    for file_path in code_files[: min(len(code_files), 300)]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        content = text[:8000]
        matches = re.findall(r"(?:from|import|require|use|include)\s*(?:\(|from)?\s*['\"]([^'\"]+)['\"]", content)
        for module in matches:
            mod = module.strip().split("/")[-1].split(".")[-1].lower()
            if module.startswith((".", "/")) or mod in base_names:
                import_link_count += 1
                if import_link_count >= 10:
                    break
        if import_link_count >= 10:
            break

    dominant_style = max(name_style_counts, key=name_style_counts.get)
    dominant_count = name_style_counts[dominant_style]
    naming_consistent = file_count >= 8 and dominant_style != "other" and (dominant_count / max(file_count, 1)) >= 0.65

    return {
        "file_count_scanned": file_count,
        "test_file_count": test_file_count,
        "import_link_count": import_link_count,
        "readme_present": readme_present,
        "changelog_present": changelog_present,
        "generated_or_dependency_dirs_detected": sorted(generated_or_dep_dirs),
        "naming_consistent": naming_consistent,
        "dominant_name_style": dominant_style,
        "name_style_counts": name_style_counts,
        "scan_truncated": file_count >= max_scan_files,
    }


def _codebase_score(
    *,
    root_info: dict[str, Any],
    manifest_markers: list[str],
    framework_markers: list[str],
    scan_signals: dict[str, Any],
) -> tuple[int, dict[str, int]]:
    breakdown = {
        "vcs_marker": 40 if root_info.get("vcs_markers") else 0,
        "language_manifest": 30 if manifest_markers else 0,
        "framework_config": 20 if framework_markers else 0,
        "test_files": 15 if int(scan_signals.get("test_file_count", 0)) > 0 else 0,
        "inter_file_imports": 15 if int(scan_signals.get("import_link_count", 0)) > 0 else 0,
        "readme_or_changelog": 10 if scan_signals.get("readme_present") or scan_signals.get("changelog_present") else 0,
        "consistent_naming": 10 if scan_signals.get("naming_consistent") else 0,
        "generated_or_dependency_dirs": 10 if scan_signals.get("generated_or_dependency_dirs_detected") else 0,
    }
    return sum(breakdown.values()), breakdown


@mcp.tool()
def analyze_code_directory(
    path: str = ".",
    threshold: int = 40,
    max_scan_files: int = 5000,
) -> dict[str, Any]:
    """
    Analyze whether a directory is a software project and return codebase confidence + signals.
    Uses root-intent markers first, then fallback content signals for ambiguous folders.
    """
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return {"ok": False, "error": "path_not_found", "path": str(target)}
    if not target.is_dir():
        return {"ok": False, "error": "path_not_directory", "path": str(target)}

    bounded_threshold = max(0, min(int(threshold), 100))
    bounded_scan_limit = max(100, min(int(max_scan_files), 20000))

    project_root, root_info = _find_project_root(target)
    manifest_markers = _manifest_markers_at(project_root)
    framework_markers = _framework_markers_at(project_root)
    project_types = _classify_project_types(project_root)
    scan_signals = _scan_code_signals(project_root, bounded_scan_limit)
    score, score_breakdown = _codebase_score(
        root_info=root_info,
        manifest_markers=manifest_markers,
        framework_markers=framework_markers,
        scan_signals=scan_signals,
    )
    is_code_directory = score >= bounded_threshold

    exclusion_dirs = set(COMMON_EXCLUDE_DIRS)
    for ptype in project_types:
        exclusion_dirs.update(EXCLUDES_BY_PROJECT_TYPE.get(ptype, set()))

    confidence_band = "low"
    if score >= 40:
        confidence_band = "high"
    elif score >= 20:
        confidence_band = "medium"

    return {
        "ok": True,
        "input_path": str(target),
        "project_root": str(project_root),
        "is_code_directory": is_code_directory,
        "confidence_score": score,
        "confidence_threshold": bounded_threshold,
        "confidence_band": confidence_band,
        "root_detection": {
            "method": root_info.get("method"),
            "vcs_markers": root_info.get("vcs_markers", []),
            "manifest_markers": manifest_markers,
            "framework_markers": framework_markers,
        },
        "project_types": project_types,
        "score_breakdown": score_breakdown,
        "signals": scan_signals,
        "indexing_guidance": {
            "scope_rule": "Index all files under project_root except excluded directories.",
            "exclude_directories": sorted(exclusion_dirs),
        },
    }


@mcp.tool()
def get_codebase_context(
    repo_path: str = ".",
    force_reindex: bool = False,
    include_all: bool = True,
    files_limit: int = 500,
    symbols_limit: int = 2000,
    threshold: int = 40,
    max_scan_files: int = 5000,
) -> dict[str, Any]:
    """
    Return full structured Layer 1 + Layer 2 codebase context for agent reasoning.

    Use this at the start of codebase tasks. It returns deterministic facts only:
    project detection/classification + indexed repository/file/symbol data.
    """
    bounded_files_limit = max(1, min(int(files_limit), 20000))
    bounded_symbols_limit = max(1, min(int(symbols_limit), 100000))
    upstream = _request_json(
        "GET",
        "/index/code/context",
        params={
            "path": repo_path,
            "force_reindex": force_reindex,
            "include_all": include_all,
            "files_limit": bounded_files_limit,
            "symbols_limit": bounded_symbols_limit,
            "threshold": max(0, min(int(threshold), 100)),
            "max_scan_files": max(100, min(int(max_scan_files), 20000)),
        },
        timeout=120,
    )
    return upstream


@mcp.tool()
def get_codebase_index(
    repo_path: str = ".",
    recent_days: int = 7,
    recent_limit: int = 20,
    symbol_limit: int = 1200,
    force_reindex: bool = False,
) -> dict[str, Any]:
    """
    First-call orientation tool.
    Returns structure, symbols index, external deps, and recent changes.
    """
    return _request_json(
        "GET",
        "/index/code/get_codebase_index",
        params={
            "path": repo_path,
            "recent_days": max(1, min(int(recent_days), 90)),
            "recent_limit": max(1, min(int(recent_limit), 100)),
            "symbol_limit": max(1, min(int(symbol_limit), 10000)),
            "force_reindex": force_reindex,
        },
        timeout=120,
    )


@mcp.tool()
def get_module_detail(
    repo_path: str,
    paths: list[str],
) -> dict[str, Any]:
    """
    Targeted module detail tool.
    Returns full symbol + import details only for requested relative paths.
    """
    cleaned = [str(p).replace("\\", "/").strip() for p in paths if str(p).strip()]
    return _request_json(
        "POST",
        "/index/code/get_module_detail",
        json_body={
            "repo_path": repo_path,
            "paths": cleaned,
        },
        timeout=120,
    )


@mcp.tool()
def get_file_content(
    repo_path: str,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    """
    Raw source read tool for specific file path and optional line range.
    """
    params: dict[str, Any] = {
        "repo_path": repo_path,
        "path": path,
        "start_line": max(1, int(start_line)),
    }
    if end_line is not None:
        params["end_line"] = max(1, int(end_line))
    return _request_json(
        "GET",
        "/index/code/get_file_content",
        params=params,
        timeout=60,
    )


@mcp.tool()
def search_code_chunks(
    repo_path: str,
    query: str,
    top_k: int = 8,
    candidate_files: int = 200,
    chunk_lines: int = 80,
    chunk_overlap: int = 20,
    max_chars: int = 1600,
    use_semantic: bool = True,
    semantic_candidates: int = 240,
    lexical_weight: float = 1.0,
    semantic_weight: float = 6.0,
) -> dict[str, Any]:
    """
    Targeted code retrieval tool.
    Returns only relevant code snippets with file + line ranges for the query.

    Use this before broad codebase context when token budget matters.
    """
    return _request_json(
        "GET",
        "/index/code/search_chunks",
        params={
            "repo_path": repo_path,
            "query": query,
            "top_k": max(1, min(int(top_k), 50)),
            "candidate_files": max(10, min(int(candidate_files), 2000)),
            "chunk_lines": max(20, min(int(chunk_lines), 400)),
            "chunk_overlap": max(0, min(int(chunk_overlap), 200)),
            "max_chars": max(200, min(int(max_chars), 4000)),
            "use_semantic": bool(use_semantic),
            "semantic_candidates": max(20, min(int(semantic_candidates), 2000)),
            "lexical_weight": max(0.0, min(float(lexical_weight), 10.0)),
            "semantic_weight": max(0.0, min(float(semantic_weight), 20.0)),
        },
        timeout=120,
    )


@mcp.tool()
def search(
    query: str,
    top_k: int = 5,
    modality: str = "all",
    session_id: str = "default",
    reset_budget: bool = False,
    include_metadata: bool = False,
) -> dict[str, Any]:
    """
    Primary retrieval tool for user data.

    WHAT IT DOES:
    - Searches indexed text, images, audio transcripts, and video context.
    - Returns ranked results with source paths and modality-specific fields.

    WHEN TO USE:
    - First call for any user question that might be answered by local content.
    - Use before answering from memory.

    HOW TO USE:
    - `query`: pass user intent directly in natural language.
    - `modality`:
      - "all" for most tasks
      - "text" / "image" / "video" / "audio" when user explicitly narrows scope
    - `top_k`: keep <= 15. Use 5 by default.
    - `include_metadata`: true when chunk metadata/source fields are needed.

    AFTER SEARCH:
    - If results are good: answer using returned evidence with citations/paths.
    - If a single result needs more detail: call `fetch_content`.
    - If results are empty/low-confidence: call `index_content`, then retry search.

    DO NOT:
    - Hallucinate answers if retrieval is empty.
    - Call index_content repeatedly for every query.
    """
    normalized_modality = modality.strip().lower()
    if normalized_modality not in {"all", "text", "image", "video", "audio"}:
        return {
            "ok": False,
            "error": "invalid_modality",
            "message": "modality must be one of: all, text, image, video, audio",
        }

    auto_reset = _should_auto_reset_budget(session_id=session_id, query=query)
    budget = _consume_budget(session_id=session_id, reset=(reset_budget or auto_reset))
    if not budget.get("ok"):
        return budget

    bounded_top_k = max(1, min(int(top_k), 15))
    upstream_top_k = max(20, bounded_top_k) if normalized_modality == "all" else bounded_top_k
    upstream = _request_json(
        "GET",
        "/search",
        params={
            "query": query,
            "top_k": upstream_top_k,
            "modality": normalized_modality,
            "text_include_metadata": include_metadata,
        },
        timeout=60,
    )
    if not upstream.get("ok"):
        return upstream

    payload = upstream["data"]
    text_results = payload.get("text", {}).get("results", []) if isinstance(payload.get("text"), dict) else []
    image_results = payload.get("image", {}).get("results", []) if isinstance(payload.get("image"), dict) else []
    video_results = payload.get("video", {}).get("results", []) if isinstance(payload.get("video"), dict) else []

    def _shape_text(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "modality": "audio" if str(r.get("category", "")).lower() == "audio" else "text",
                    "source": r.get("path"),
                    "filename": r.get("filename"),
                    "score": float(r.get("score", 0.0)),
                    "category": r.get("category"),
                    "chunk": r.get("chunk"),
                    "chunk_id": r.get("chunk_id"),
                    "chunk_index": r.get("chunk_index"),
                    "chunk_total": r.get("chunk_total"),
                }
            )
        return out

    def _shape_images(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in rows:
            final_score = float(r.get("final_score", r.get("score", 0.0)))
            out.append(
                {
                    "modality": "image",
                    "source": r.get("path"),
                    "filename": r.get("filename"),
                    "score": final_score,
                    "final_score": final_score,
                    "semantic_score": float(r.get("semantic_score", 0.0)),
                    "ocr_score": float(r.get("ocr_score", 0.0)),
                    "filename_score": float(r.get("filename_score", 0.0)),
                    "match_type": r.get("match_type"),
                    "ocr_text": r.get("ocr_text", ""),
                    "ocr_snippet": r.get("ocr_snippet", ""),
                    "capabilities": r.get("capabilities", {}),
                }
            )
        return out

    def _shape_videos(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "modality": "video",
                    "source": r.get("video_path"),
                    "score": float(r.get("score", 0.0)),
                    "description": r.get("description", ""),
                    "best_timestamp": r.get("best_timestamp"),
                    "transcript_match": r.get("transcript_match", False),
                    "context_match": r.get("context_match", False),
                    "ocr_text": r.get("ocr_text", ""),
                }
            )
        return out

    text_shaped = _shape_text(text_results)
    image_shaped = _shape_images(image_results)
    video_shaped = _shape_videos(video_results)

    if normalized_modality == "all":
        merged = text_shaped + image_shaped + video_shaped
    elif normalized_modality == "text":
        merged = [r for r in text_shaped if r["modality"] == "text"]
    elif normalized_modality == "audio":
        merged = [r for r in text_shaped if r["modality"] == "audio"]
    elif normalized_modality == "image":
        merged = image_shaped
    else:
        merged = video_shaped

    merged.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    merged = merged[:bounded_top_k]

    return {
        "ok": True,
        "query": query,
        "modality": normalized_modality,
        "top_k": bounded_top_k,
        "session_id": budget.get("session_id"),
        "budget_remaining": budget.get("budget_remaining"),
        "budget_max": budget.get("budget_max"),
        "result_count": len(merged),
        "results": merged,
        "empty_or_low_confidence": (not merged) or all(float(r.get("score", 0.0)) < 0.1 for r in merged),
    }


@mcp.tool()
def fetch_content(
    path: str,
    modality: str = "auto",
) -> dict[str, Any]:
    """
    Secondary retrieval tool for file-level detail.

    USE THIS WHEN:
    - `search` found a relevant file and you need more context from that file.
    - You need transcript/frame details for video.
    - You need OCR text or metadata for an image.

    INPUTS:
    - `path`: absolute file path from `search` results.
    - `modality`:
      - "auto" (recommended)
      - or explicit "video" / "text" / "image"

    OUTPUT:
    - Video: frame timeline + transcript excerpt (if indexed).
    - Text/audio: indexed textual content.
    - Image: OCR text and metadata when available.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"ok": False, "error": "file_not_found", "path": str(p)}

    # Auto-detect modality
    if modality == "auto":
        ext = p.suffix.lower()
        if ext in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
            modality = "video"
        elif ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}:
            modality = "image"
        else:
            modality = "text"

    if modality == "video":
        return _fetch_video_content(str(p))
    elif modality == "image":
        return _fetch_image_content(str(p))
    else:
        return _fetch_text_content(str(p))


def _fetch_image_content(image_path: str) -> dict[str, Any]:
    """Retrieve indexed OCR context for an image when available."""
    try:
        image_db = _STORAGE_DIR / "image_search_implementation_v2" / "storage" / "images_meta.db"
        if not image_db.exists():
            return {
                "ok": True,
                "modality": "image",
                "path": image_path,
                "filename": Path(image_path).name,
                "ocr_text": "",
                "ocr_available": False,
                "next_step": "Use prepare_file_for_tool to access this image.",
            }

        conn = sqlite3.connect(str(image_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                i.filename,
                COALESCE(i.ocr_text, '') AS ocr_text
            FROM images i
            WHERE i.path = ?
            """,
            (image_path,),
        ).fetchone()
        conn.close()

        if not row:
            return {
                "ok": True,
                "modality": "image",
                "path": image_path,
                "filename": Path(image_path).name,
                "ocr_text": "",
                "ocr_available": False,
                "next_step": "Use prepare_file_for_tool to access this image.",
            }

        ocr_text = (row["ocr_text"] or "").strip()
        return {
            "ok": True,
            "modality": "image",
            "path": image_path,
            "filename": row["filename"] or Path(image_path).name,
            "ocr_text": ocr_text[:3000],
            "ocr_available": bool(ocr_text),
            "next_step": "Use prepare_file_for_tool to access this image.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fetch_video_content(video_path: str) -> dict[str, Any]:
    """Retrieve indexed frame descriptions and transcript for a video."""
    try:
        video_db = _STORAGE_DIR / "video_search_implementation_v2" / "storage" / "videos_meta.db"
        if not video_db.exists():
            return {"ok": False, "error": "video_index_not_found"}

        import sqlite3
        conn = sqlite3.connect(str(video_db))
        conn.row_factory = sqlite3.Row

        video_row = conn.execute(
            "SELECT id FROM videos WHERE path = ?", (video_path,)
        ).fetchone()
        if not video_row:
            conn.close()
            return {"ok": False, "error": "video_not_indexed", "path": video_path}

        frames = conn.execute(
            "SELECT timestamp, description, ocr_text FROM frames WHERE video_id = ? ORDER BY timestamp",
            (video_row["id"],),
        ).fetchall()
        conn.close()

        frame_summaries = []
        for f in frames:
            ts = f["timestamp"]
            ts_str = f"{ts:.1f}s" if ts and ts >= 0 else "unknown"
            frame_summaries.append({
                "timestamp": ts_str,
                "description": f["description"] or "no description",
                "ocr_text": (f["ocr_text"] or "").strip(),
            })

        # Try to get transcript from text FTS
        transcript = None
        try:
            text_db = _STORAGE_DIR / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db"
            if text_db.exists():
                tconn = sqlite3.connect(str(text_db))
                tconn.row_factory = sqlite3.Row
                trow = tconn.execute(
                    "SELECT content FROM files WHERE path = ? AND category = 'video_transcript'",
                    (video_path,),
                ).fetchone()
                if trow:
                    transcript = trow["content"]
                tconn.close()
        except Exception:
            pass

        return {
            "ok": True,
            "modality": "video",
            "path": video_path,
            "filename": Path(video_path).name,
            "frame_count": len(frame_summaries),
            "frames": frame_summaries[:20],  # Cap at 20 for context window
            "transcript_available": transcript is not None,
            "transcript_excerpt": (transcript[:2000] + "...") if transcript and len(transcript) > 2000 else transcript,
            "context_available": any(frame.get("description") or frame.get("ocr_text") for frame in frame_summaries),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fetch_text_content(file_path: str) -> dict[str, Any]:
    """Retrieve indexed text content for a file."""
    try:
        text_db = _STORAGE_DIR / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db"
        if not text_db.exists():
            return {"ok": False, "error": "text_index_not_found"}

        import sqlite3
        conn = sqlite3.connect(str(text_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT filename, category, content FROM files WHERE path = ?",
            (file_path,),
        ).fetchone()
        conn.close()

        if not row:
            return {"ok": False, "error": "file_not_indexed", "path": file_path}

        content = row["content"] or ""
        return {
            "ok": True,
            "modality": "text",
            "path": file_path,
            "filename": row["filename"],
            "category": row["category"],
            "content": content[:5000],  # Cap for context window
            "content_truncated": len(content) > 5000,
            "total_length": len(content),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_files(
    directory: str,
    recursive: bool = True,
    limit: int = 100,
    pattern: str = "*",
) -> dict[str, Any]:
    """
    List files from a local directory (served by backend /files/list) and include fetchable URLs.
    """
    bounded_limit = max(1, min(int(limit), 1000))
    upstream = _request_json(
        "GET",
        "/files/list",
        params={
            "directory": directory,
            "recursive": recursive,
            "limit": bounded_limit,
            "pattern": pattern,
        },
        timeout=30,
    )
    if not upstream.get("ok"):
        return upstream

    data = upstream["data"]
    rows = data.get("files", []) if isinstance(data, dict) else []
    out = []
    for r in rows:
        p = r.get("path")
        out.append(
            {
                "path": p,
                "filename": r.get("filename"),
                "size_bytes": r.get("size_bytes"),
                "mime_type": r.get("mime_type"),
                "mtime": r.get("mtime"),
            }
        )

    return {
        "ok": True,
        "directory": data.get("directory"),
        "count": len(out),
        "files": out,
    }


@mcp.tool()
def reveal_file(path: str) -> dict[str, Any]:
    """
    Open the OS file manager with the target file selected so user can drag/drop it into chat.
    """
    return _reveal_file_in_explorer(Path(path).expanduser().resolve())


@mcp.tool()
def filesystem_access_profile(tool: str) -> dict[str, Any]:
    """
    Return filesystem access mode guidance for a client tool.
    """
    t = _normalize_tool_name(tool)
    if t in LOCAL_FILESYSTEM_TOOLS:
        return {
            "ok": True,
            "tool": t,
            "access_mode": "direct_local_filesystem",
            "can_read_absolute_paths": True,
            "recommended_flow": "return absolute path and let tool open/read directly",
        }
    if t in REMOTE_ONLY_TOOLS:
        return {
            "ok": True,
            "tool": t,
            "access_mode": "remote_no_local_filesystem",
            "can_read_absolute_paths": False,
            "recommended_flow": "reveal file in OS explorer and ask user to drag/drop upload",
        }
    return {
        "ok": True,
        "tool": t,
        "access_mode": "unknown",
        "can_read_absolute_paths": False,
        "recommended_flow": "assume remote unless verified; use reveal + drag/drop flow",
    }


@mcp.tool()
def prepare_file_for_tool(path: str, tool: str) -> dict[str, Any]:
    """
    File handoff helper for tool compatibility.

    USE THIS WHEN:
    - User wants to open/attach/send a local file into a client tool.
    - You need to adapt behavior based on whether the tool has local FS access.

    BEHAVIOR:
    - Local filesystem tools: returns direct absolute path.
    - Remote/web tools: opens system file manager for drag-drop workflow.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"ok": False, "error": "file_not_found", "path": str(p)}

    profile = filesystem_access_profile(tool)
    if profile.get("can_read_absolute_paths"):
        return {
            "ok": True,
            "tool": profile.get("tool"),
            "access_mode": profile.get("access_mode"),
            "path": str(p),
            "next_step": "Tool can read this path directly.",
        }

    revealed = _reveal_file_in_explorer(p)
    return {
        "ok": bool(revealed.get("ok")),
        "tool": profile.get("tool"),
        "access_mode": profile.get("access_mode"),
        "path": str(p),
        "reveal": revealed,
        "next_step": "File manager opened. Ask user to drag/drop this file into chat.",
    }


@mcp.tool()
def refine_search(
    original_query: str,
    reason: str,
    refined_query: str,
    exclude_sources: list[str] | None = None,
    top_k: int = 5,
    modality: str = "text",
    session_id: str = "default",
    include_metadata: bool = False,
) -> dict[str, Any]:
    """
    Query refinement tool for iterative retrieval.

    USE THIS WHEN:
    - Initial search is too broad, noisy, or misses intent.
    - You have a clear reason for refinement (e.g., add modality terms,
      constrain source, remove ambiguity).

    INPUTS:
    - `original_query`: the first user query.
    - `reason`: why refinement is needed.
    - `refined_query`: improved query to execute.
    - `exclude_sources`: optional paths/sources to exclude.

    OUTPUT:
    - New retrieval payload plus session budget state.
    """
    budget = _consume_budget(session_id=session_id, reset=False)
    if not budget.get("ok"):
        return budget

    _log_refine_feedback(
        session_id=session_id,
        original_query=original_query,
        reason=reason,
        refined_query=refined_query,
        exclude_sources=exclude_sources,
    )

    normalized_modality = modality.strip().lower()
    if normalized_modality not in {"all", "text", "image", "video", "audio"}:
        return {
            "ok": False,
            "error": "invalid_modality",
            "message": "modality must be one of: all, text, image, video, audio",
        }

    bounded_top_k = max(1, min(int(top_k), 15))
    params: dict[str, Any] = {
        "query": refined_query,
        "top_k": max(20, bounded_top_k) if normalized_modality == "all" else bounded_top_k,
        "modality": normalized_modality,
        "text_include_metadata": include_metadata,
    }
    if exclude_sources:
        params["exclude_sources"] = ",".join(str(s) for s in exclude_sources if str(s).strip())

    upstream = _request_json("GET", "/search", params=params, timeout=60)
    if not upstream.get("ok"):
        return upstream

    return {
        "ok": True,
        "original_query": original_query,
        "reason": reason,
        "query": refined_query,
        "session_id": budget.get("session_id"),
        "budget_remaining": budget.get("budget_remaining"),
        "budget_max": budget.get("budget_max"),
        "data": upstream["data"],
    }


@mcp.tool()
def get_neighbors(
    chunk_id: str,
    direction: str = "next",
    count: int = 1,
    session_id: str = "default",
) -> dict[str, Any]:
    budget = _consume_budget(session_id=session_id, reset=False)
    if not budget.get("ok"):
        return budget

    upstream = _request_json(
        "GET",
        "/search/text/neighbors",
        params={
            "chunk_id": chunk_id,
            "direction": direction,
            "count": max(1, min(int(count), 5)),
        },
        timeout=30,
    )
    if not upstream.get("ok"):
        return upstream

    return {
        "ok": True,
        "session_id": budget.get("session_id"),
        "budget_remaining": budget.get("budget_remaining"),
        "budget_max": budget.get("budget_max"),
        "data": upstream["data"],
    }


@mcp.tool()
def index_content(
    run_text: bool = True,
    run_image: bool = True,
    run_video: bool = True,
    run_audio: bool = True,
    target_dir: str | None = None,
) -> dict[str, Any]:
    """
    Trigger background indexing for new/stale content.

    USE THIS WHEN:
    - User explicitly asks to reindex/refresh/scan.
    - Search results are missing stale or expected files.
    - User asks to index a specific folder (`target_dir`).

    HOW TO USE:
    - Keep all modalities enabled for full refresh.
    - Set only required modalities when user asks for targeted indexing.
    - Use `target_dir` to constrain indexing scope.

    DO NOT:
    - Trigger this on every query.
    - Loop index calls back-to-back without user intent.
    """
    params = {
        "run_text": run_text,
        "run_image": run_image,
        "run_video": run_video,
        "run_audio": run_audio,
    }
    if target_dir:
        params["target_dir"] = target_dir

    return _request_json(
        "POST",
        "/index/scan",
        params=params,
        timeout=30,
    )


@mcp.tool()
def list_sources() -> dict[str, Any]:
    """
    Source and index inventory tool.

    USE THIS WHEN:
    - User asks what is indexed, which folders are configured, or connector status.
    - You need quick diagnostics before deciding whether to reindex.

    RETURNS:
    - Configured source folders per modality.
    - Indexed item counts (text/audio/image/video/total).
    - Image backend status/capabilities snapshot from backend.
    """
    try:
        from config import get_organized_root, get_video_directories, get_audio_directories, get_image_directory
        base_dir = str(get_organized_root())
        text_folders = ["docs", "spreadsheets", "code"]
        
        def _rel(p):
            try: return str(p.relative_to(get_organized_root()))
            except ValueError: return str(p)
            
        folders_cfg = {
            "text": [str(get_organized_root() / f) for f in text_folders],
            "images": str(get_image_directory()),
            "videos": [str(p) for p in get_video_directories()],
            "audio": [str(p) for p in get_audio_directories()],
        }
    except Exception:
        base_dir = "."
        folders_cfg = {}

    text_db = _STORAGE_DIR / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db"
    image_db = _STORAGE_DIR / "image_search_implementation_v2" / "storage" / "images_meta.db"
    video_db = _STORAGE_DIR / "video_search_implementation_v2" / "storage" / "videos_meta.db"

    text_total = _safe_sql_count(text_db, "SELECT COUNT(*) FROM files")
    audio_total = _safe_sql_count(text_db, "SELECT COUNT(*) FROM files WHERE LOWER(category) = 'audio'")
    doc_text_total = max(0, text_total - audio_total)
    image_total = _safe_sql_count(image_db, "SELECT COUNT(*) FROM images")
    video_total = _safe_sql_count(video_db, "SELECT COUNT(*) FROM videos")

    image_status = _request_json("GET", "/image/index/status", timeout=10)
    image_index_status: dict[str, Any] = {}
    semantic_available = False
    if image_status.get("ok"):
        image_index_status = dict(image_status["data"])
        semantic_available = bool(
            image_index_status.get("capabilities", {}).get("semantic_backend_available")
        )

    return {
        "ok": True,
        "sources": {
            "base_dir": base_dir,
            "folders": folders_cfg,
            "connectors": {
                "filesystem": True,
                "annoy": semantic_available,
                "rclone": True,
            },
        },
        "indexed_counts": {
            "text": doc_text_total,
            "audio": audio_total,
            "image": image_total,
            "video": video_total,
            "total": doc_text_total + audio_total + image_total + video_total,
        },
        "backend": {
            "base_url": BACKEND_BASE_URL,
            "image_index_status": image_index_status,
        },
    }


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
        return
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()



