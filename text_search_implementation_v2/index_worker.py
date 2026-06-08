# text_search_implementation_v2/index_worker.py
import argparse
import os
from pathlib import Path
from text_search_implementation_v2.db import (
    delete_files_under,
    get_file_mtime,
    init_db,
    upsert_file,
)
from text_search_implementation_v2.extract import extract_text
from text_search_implementation_v2.config import BASE_DIR, TEXT_FOLDERS


SUPPORTED_EXT = {
    "txt",
    "md",
    "pdf",
    "doc",
    "docx",
    "ppt",
    "pptx",
    "csv",
    "xlsx",
    "xls",
    "json",
    "xml",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "conf",
    "log",
    "html",
    "htm",
    "rst",
    "tsv",
    "rtf",
    "ods",
}

CODE_ROOT_MARKERS = {
    ".git",
    ".hg",
    ".svn",
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
    "next.config.js",
    "vite.config.js",
    "webpack.config.js",
    "tsconfig.json",
    "angular.json",
    "vue.config.js",
    "pytest.ini",
}
CODE_ROOT_GLOB_MARKERS = ("*.csproj", "*.sln")
COMMON_NON_TEXT_DIRS = {
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
    ".pnpm-store",
    ".yarn",
    ".npm",
    ".turbo",
    ".cache",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".gradle",
    "obj",
    "bin",
}
NON_TEXT_CATEGORIES = {"audio", "video_transcript"}


def _looks_like_code_directory(path: Path) -> bool:
    if not path.is_dir():
        return False

    for marker in CODE_ROOT_MARKERS:
        if (path / marker).exists():
            return True
    for pattern in CODE_ROOT_GLOB_MARKERS:
        if any(path.glob(pattern)):
            return True
    return False


def _purge_text_rows_under(root: Path) -> int:
    return delete_files_under(root, excluded_categories=NON_TEXT_CATEGORIES)


def index_one_file(p: Path):
    try:
        current_mtime = p.stat().st_mtime
    except Exception:
        return False

    # Check existing DB record before extracting text
    existing_mtime = get_file_mtime(str(p))
    if existing_mtime is not None:
        # skip unchanged file
        if abs(existing_mtime - current_mtime) < 0.001:
            return False

    # Only extract if new or modified
    content = extract_text(p)
    if not content:
        return False

    upsert_file(str(p), p.name, p.parent.name, current_mtime, content)
    return True


def full_scan(target_dir: str | None = None):
    init_db()
    total = 0
    skipped = 0
    pruned_code_dirs = 0
    purged_rows = 0

    if target_dir:
        # When a specific directory is given, scan it recursively regardless of subfolder structure
        scan_paths = [Path(target_dir).expanduser().resolve()]
    else:
        # Default: use configured BASE_DIR + known subfolders
        scan_paths = [BASE_DIR / folder for folder in TEXT_FOLDERS]

    for folder_path in scan_paths:
        if not folder_path.exists():
            print(f"Scan path not found, skipping: {folder_path}")
            continue

        if _looks_like_code_directory(folder_path):
            pruned_code_dirs += 1
            purged_rows += _purge_text_rows_under(folder_path)
            continue

        for dirpath, dirnames, filenames in os.walk(folder_path):
            current_dir = Path(dirpath)

            kept_dirs = []
            for dirname in dirnames:
                child = current_dir / dirname
                if dirname in COMMON_NON_TEXT_DIRS:
                    continue
                if _looks_like_code_directory(child):
                    pruned_code_dirs += 1
                    purged_rows += _purge_text_rows_under(child)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                path = current_dir / filename
                if path.suffix.lower().lstrip(".") not in SUPPORTED_EXT:
                    continue

                if index_one_file(path):
                    total += 1
                else:
                    skipped += 1

    print(
        f"Indexed files: {total}, Skipped unchanged: {skipped}, "
        f"Pruned code dirs: {pruned_code_dirs}, Removed stale text rows: {purged_rows}"
    )


def run_scan(target_dir: str | None = None):
    full_scan(target_dir=target_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=str, help="Single file to index")
    parser.add_argument("--scan", action="store_true", help="Do a full startup scan")
    parser.add_argument("--dir", type=str, help="Directory to scan (overrides config)")
    args = parser.parse_args()

    init_db()

    if args.file:
        p = Path(args.file)
        print("Indexing:", p)
        ok = index_one_file(p)
        print("Indexed", ok)

    elif args.scan:
        full_scan(target_dir=args.dir)

    else:
        print("Nothing to do. Use --file or --scan [--dir /path]")


if __name__ == "__main__":
    main()
