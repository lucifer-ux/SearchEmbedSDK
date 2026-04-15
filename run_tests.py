"""
run_tests.py - Simple ASCII test runner for the implementation verification.
Works around Windows terminal encoding issues.
"""
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0
skipped = 0
results = []


def test(name, fn):
    global passed, failed, skipped
    try:
        test_result = fn()
        if test_result == "SKIP":
            results.append(("SKIP", name, "dependency not installed"))
            skipped += 1
        else:
            results.append(("PASS", name, ""))
            passed += 1
    except Exception as e:
        results.append(("FAIL", name, str(e)))
        failed += 1

# ---- STEP 1: Config module ----
def t_config_import():
    from config import (get_video_directories, get_audio_directories,
                        get_image_directory, get_organized_root, get_dedup_threshold)
    return True
test("config.py imports correctly", t_config_import)

def t_config_paths():
    from config import get_video_directories, get_image_directory
    from pathlib import Path
    vids = get_video_directories()
    assert isinstance(vids, list) and all(isinstance(p, Path) for p in vids)
    img = get_image_directory()
    assert isinstance(img, Path)
test("config returns valid Path objects", t_config_paths)

def t_dedup():
    from config import get_dedup_threshold
    t = get_dedup_threshold()
    assert t == 0.85, "expected 0.85, got %s" % t
test("dedup threshold defaults to 0.85", t_dedup)

# ---- STEP 2: No hardcoded paths ----
def t_no_hardcoded_unimain():
    c = open("unimain.py", "r", encoding="utf-8").read()
    assert "/mnt/storage/organized_files/video" not in c
    assert "/mnt/storage/organized_files/audio" not in c
test("unimain.py no hardcoded /mnt/storage paths", t_no_hardcoded_unimain)

def t_no_hardcoded_worker():
    c = open("audio_search_implementation_v2/worker.py", "r", encoding="utf-8").read()
    assert "/mnt/storage/organized_files" not in c
test("worker.py no hardcoded paths", t_no_hardcoded_worker)

# ---- STEP 3: sqlite-vec ----
def t_sqlite_vec():
    try:
        import sqlite_vec
        return True
    except ImportError:
        return "SKIP"
test("sqlite-vec is importable", t_sqlite_vec)

# ---- STEP 4: Video index structure ----
def t_video_mmr():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "mmr_is_unique" in c, "no MMR function"
    assert "selected_vecs" in c, "no MMR selected_vecs"
test("video_index has MMR dedup", t_video_mmr)

def t_video_no_annoy():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "AnnoyIndex" not in c, "Annoy still present"
    # Check that .npy is not used in actual code (ignore comments that describe the fix)
    code_lines = [l for l in c.splitlines() if l.strip() and not l.strip().startswith("#")]
    code_only = "\n".join(code_lines)
    assert ".npy" not in code_only, ".npy still used in code"
    assert "frame_vectors" in c, "sqlite-vec table missing"
test("video_index uses sqlite-vec (no Annoy)", t_video_no_annoy)

def t_video_transcript():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "transcribe" in c.lower(), "no transcript"
    assert "video_transcript" in c, "no video_transcript category"
test("video_index has transcript integration", t_video_transcript)

def t_video_describe():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "_describe_frame" in c, "no describe_frame"
    assert "description" in c, "no description field"
test("video_index has frame descriptions", t_video_describe)

def t_video_atomic():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "conn.rollback()" in c, "no rollback"
    assert "DELETE FROM frames WHERE video_id" in c, "no frame cleanup"
test("video_index has atomic cleanup", t_video_atomic)

def t_video_topk():
    c = open("video_search_implementation_v2/video_index.py", "r", encoding="utf-8").read()
    assert "top_k: int = 5" in c, "top_k default not 5"
test("video search top_k defaults to 5", t_video_topk)

# ---- STEP 5: Watcher ----
def t_watcher_exists():
    assert os.path.exists("video_search_implementation_v2/watcher.py")
    c = open("video_search_implementation_v2/watcher.py", "r", encoding="utf-8").read()
    assert "start_video_watcher" in c
    assert "stop_video_watcher" in c
test("watcher.py exists with API", t_watcher_exists)

def t_watchdog():
    try:
        import watchdog
        return True
    except ImportError:
        return "SKIP"
test("watchdog is importable", t_watchdog)

# ---- STEP 6: SDK ----
def t_sdk():
    assert os.path.exists("core/__init__.py")
    assert os.path.exists("core/sdk.py")
    c = open("core/sdk.py", "r", encoding="utf-8").read()
    for m in ["class ContextCore", "def embed_text", "def search(",
              "def index_directory", "def start_watcher"]:
        assert m in c, "Missing: %s" % m
test("SDK core/sdk.py has ContextCore", t_sdk)

# ---- STEP 7: MCP ----
def t_mcp_fetch():
    c = open("mcp_server.py", "r", encoding="utf-8").read()
    assert "def fetch_content" in c, "fetch_content missing"
    assert "_fetch_video_content" in c, "video content fetch missing"
test("MCP has fetch_content tool", t_mcp_fetch)

def t_mcp_video_shape():
    c = open("mcp_server.py", "r", encoding="utf-8").read()
    assert '"description"' in c
    assert '"best_timestamp"' in c
test("MCP video results have description+timestamp", t_mcp_video_shape)

def t_mcp_image_shape_ocr():
    c = open("mcp_server.py", "r", encoding="utf-8").read()
    assert '"match_type"' in c
    assert '"ocr_text"' in c
    assert '"ocr_snippet"' in c
test("MCP image results include OCR + match metadata", t_mcp_image_shape_ocr)

def t_image_search_annoy_sqlite_wired():
    c = open("unimain.py", "r", encoding="utf-8").read()
    assert "image_search_implementation_v2.search" in c
    assert "annoy_sqlite_ocr" in c
    assert "semantic_backend\": \"annoy_sqlite\"" in c
test("unimain image search is wired to Annoy+SQLite runtime", t_image_search_annoy_sqlite_wired)

def t_image_status_annoy_fields():
    c = open("unimain.py", "r", encoding="utf-8").read()
    assert '"indexed_images_with_ocr"' in c
    assert '"ocr_coverage"' in c
    assert '"engine": "annoy_sqlite_ocr"' in c
    assert '"annoy_needs_rebuild"' in c
test("image index status exposes Annoy+OCR fields", t_image_status_annoy_fields)

def t_image_result_score_fields():
    c = open("image_search_implementation_v2/search.py", "r", encoding="utf-8").read()
    assert '"semantic_score"' in c
    assert '"ocr_score"' in c
    assert '"filename_score"' in c
    assert '"final_score"' in c
test("image search emits merged score components", t_image_result_score_fields)

# ---- STEP 8: Requirements ----
def t_requirements():
    c = open("requirements.txt", "r", encoding="utf-8").read()
    assert "sqlite-vec" in c
    assert "watchdog" in c
test("requirements.txt has sqlite-vec + watchdog", t_requirements)

# ---- PRINT RESULTS ----
print("")
print("=" * 55)
print("  VERIFICATION RESULTS")
print("=" * 55)
for status, name, detail in results:
    marker = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
    line = "  %s %s" % (marker, name)
    if detail:
        line += " -- %s" % detail
    print(line)

print("")
print("  Passed: %d  |  Failed: %d  |  Skipped: %d" % (passed, failed, skipped))
print("=" * 55)

if failed > 0:
    print("")
    print("  Some tests FAILED. Review the output above.")
    sys.exit(1)
elif skipped > 0:
    print("")
    print("  Some deps not installed. Run:")
    print("    .venv\\Scripts\\pip.exe install sqlite-vec watchdog")
    print("  Then re-run: .venv\\Scripts\\python.exe run_tests.py")
else:
    print("")
    print("  All tests passed!")
