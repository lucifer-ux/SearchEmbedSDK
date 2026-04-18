# unimain.py
import os
import time
import asyncio
import threading
import sqlite3
import traceback
import queue
import subprocess
import gc
import hashlib
import base64
import shutil
import sys
import platform
import builtins
import fnmatch
import mimetypes
import importlib.util
import re
import json
import ast
from pathlib import Path
from typing import Optional, Any
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from activity.recent_sync import get_recent_syncs
from activity.search_analytics import record_search_hits
from index_controller.thumbnail_manager import read_thumbnail
from index_controller.ignore import should_ignore
from fastapi import Request
from rclone_service import *
from auth_manager import start_auth, auth_sessions
from config import (
    get_video_directories,
    get_audio_directories,
    get_image_directory,
    get_organized_root,
    get_watch_directories,
    get_code_directories,
    get_enable_text,
    get_enable_image,
    get_enable_audio,
    get_enable_video,
    get_enable_code,
    get_storage_dir,
)
from cli.lifecycle import acquire_index_lock, index_lock_active, release_index_lock, update_index_state

_orig_print = builtins.print

def print(*args, **kwargs):
    try:
        return _orig_print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = [str(a).encode("ascii", "replace").decode("ascii") for a in args]
        return _orig_print(*safe_args, **kwargs)


# ── Paths & tunables ─────────────────────────────────────────
ROOT            = Path(__file__).parent.resolve()
ORGANIZED_ROOT  = get_organized_root()
IMAGE_DIR       = get_image_directory()

WIFI_INTERFACE = "wlan0"
HOTSPOT_NAME = "RadxaAP"
HOTSPOT_SSID = "RadxaSetup"
HOTSPOT_PASSWORD = "radxa1234"
HOTSPOT_IP = "10.99.0.1/24"

STORAGE_DIR      = get_storage_dir()
IMAGE_INDEX_DIR  = STORAGE_DIR / "image_search_implementation_v2" / "storage"
IMAGE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_META_DB    = IMAGE_INDEX_DIR / "images_meta.db"
IMAGE_EMBED_DIR  = IMAGE_INDEX_DIR / "embeddings"
IMAGE_EMBED_DIR.mkdir(parents=True, exist_ok=True)
ANNOY_INDEX_PATH = IMAGE_INDEX_DIR / "annoy_index.ann"

ANNOY_DIM    = 512
ANNOY_N_TREES = 10
IMAGE_REBUILD_LOCK = threading.Lock()
IMAGE_EMBED_BATCH_REBUILD_THRESHOLD = 8
SEARCH_THREADPOOL = 2
SCAN_THREADPOOL   = 2

# llama-server — same build dir as llama-cli
LLAMA_SERVER_BIN  = ROOT / "llama.cpp" / "build" / "bin" / "llama-server"
LLAMA_MODEL       = ROOT / "llama.cpp" / "models" / "rocket-3B" / "rocket-3b.Q4_K_M.gguf"
LLAMA_SERVER_HOST = "127.0.0.1"
LLAMA_SERVER_PORT = 8765          # internal only, not exposed
LLAMA_SERVER_URL  = f"http://{LLAMA_SERVER_HOST}:{LLAMA_SERVER_PORT}"
LLAMA_CTX         = 1024
LLAMA_THREADS     = 4

# Queued requests wait up to this long before getting a 503
QUEUE_WAIT_TIMEOUT = 60   # seconds
THERMAL_LIMIT_C = 80  # °C — reject LLM calls above this

NETWORK_STATE = {
    "connected": False,
    "ssid": None,
    "hotspot_active": False
}
NETWORK_CONTROL_SUPPORTED = (
    os.name == "posix" and shutil.which("nmcli") is not None
)

# File access policy:
# - default allows arbitrary local file serving/listing for desktop/dev workflows
# - can be restricted by setting CONTEXTCORE_ALLOW_ARBITRARY_FILE_ACCESS=0 and
#   CONTEXTCORE_FILE_ROOTS to a ';'-separated allowlist.
ALLOW_ARBITRARY_FILE_ACCESS = (
    os.getenv("CONTEXTCORE_ALLOW_ARBITRARY_FILE_ACCESS", "1").strip().lower()
    not in {"0", "false", "no"}
)
FILE_ROOTS = [
    Path(p).expanduser().resolve()
    for p in os.getenv("CONTEXTCORE_FILE_ROOTS", "").split(";")
    if p.strip()
]

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
COMMON_CODE_EXCLUDE_DIRS = {
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
    ".nuxt",
    ".svelte-kit",
    ".gradle",
    "obj",
    "bin",
    ".cargo",
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
DEPENDENCY_DIR_NAMES = {
    "node_modules", "vendor", ".venv", "venv", ".pnpm-store", ".yarn", ".npm", ".cargo"
}
GENERATED_DIR_NAMES = {
    "dist", "build", "out", "target", "coverage", ".cache", ".parcel-cache", ".next", ".nuxt", ".svelte-kit"
}

CODE_INDEX_DB = STORAGE_DIR / "storage" / "code_index_layer1.db"
CODE_CHUNK_ANNOY_PATH = STORAGE_DIR / "storage" / "code_chunks.ann"
CODE_CHUNK_ANNOY_STATE_PATH = STORAGE_DIR / "storage" / "code_chunks_annoy_state.json"
CODE_CHUNK_LINES = 80
CODE_CHUNK_OVERLAP = 20
CODE_INDEX_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
CODE_TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".swift",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".sql", ".sh", ".bash", ".zsh", ".ps1", ".toml", ".yaml", ".yml",
    ".json", ".xml", ".ini", ".cfg", ".conf", ".env",
    ".md", ".txt",
}
CODE_SPECIAL_FILENAMES = {
    "Dockerfile", "Makefile", "CMakeLists.txt", "Jenkinsfile", "Procfile",
}
TEXT_WATCH_EXTS = {
    ".txt", ".md", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".csv",
    ".xlsx", ".xls", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".log", ".html", ".htm", ".rst", ".tsv", ".rtf", ".ods",
}
IMAGE_WATCH_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
AUDIO_WATCH_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_WATCH_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
CODE_WATCH_EXTS = CODE_TEXT_EXTENSIONS - {".md", ".txt"}

_watch_queue: queue.Queue = queue.Queue()
_watch_stop_event = threading.Event()
_watch_worker_thread: Optional[threading.Thread] = None
_watch_observer = None
_watch_debounce_lock = threading.Lock()
_watch_last_job: dict[tuple[str, str], float] = {}

# Watcher logging
_WATCHER_LOG_PATH = get_storage_dir() / "watcher.log"

def _watcher_log(msg: str) -> None:
    """Log watcher events to file for debugging."""
    from datetime import datetime
    timestamp = datetime.now().isoformat()
    try:
        with open(_WATCHER_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass  # Fail silently if logging fails

# NOTE: No idle-unload — whichever family is loaded stays loaded
#       until the OTHER family is explicitly requested.


# ═══════════════════════════════════════════════════════════════
#  ResourceManager
#  ─────────────────────────────────────────────────────────────
#  Rules:
#    • Only one model family (LLM or Embed) is in RAM at a time.
#    • On a family switch → the resident family is cleanly unloaded
#      BEFORE the new family starts loading.
#    • Concurrent requests for the SAME family: second request waits
#      on the asyncio.Lock and then runs immediately (no reload needed
#      because the same family is still active).
#    • Concurrent requests for DIFFERENT families: the latecomer waits
#      up to QUEUE_WAIT_TIMEOUT seconds; if still blocked → HTTP 503.
#    • No watchdog / idle timer — models stay loaded indefinitely.
# ═══════════════════════════════════════════════════════════════
class ResourceManager:
    STATE_IDLE  = "idle"
    STATE_LLM   = "llm"
    STATE_EMBED = "embed"

    def __init__(self):
        # Single lock — only one "activation" may proceed at a time.
        # Once activated, the lock is released so the same-family
        # requests can stack without re-triggering a model switch.
        self._switch_lock = asyncio.Lock()
        self._state       = self.STATE_IDLE

        self._llm_proc: Optional[subprocess.Popen] = None
        self._llm_ready  = False

    # ── context managers used by endpoint handlers ─────────────

    @asynccontextmanager
    async def llm_context(self):
        """
        Guarantee llama-server is running.
        If embed models are currently loaded, unload them first.
        Multiple concurrent LLM calls are fine — they share the
        already-running server with no extra switching overhead.
        """
        # Only lock during the *switch* phase; release before yielding
        # so parallel LLM calls don't serialise each other needlessly.
        if self._state != self.STATE_LLM:
            try:
                await asyncio.wait_for(
                    self._switch_lock.acquire(), timeout=QUEUE_WAIT_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise HTTPException(
                    503, "Server busy — timed out waiting for LLM slot (60 s)"
                )
            try:
                # Double-check: another coroutine may have already switched
                if self._state != self.STATE_LLM:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._activate_llm
                    )
                    self._state = self.STATE_LLM
            finally:
                self._switch_lock.release()

        yield   # llama-server is up; caller does its work

    @asynccontextmanager
    async def embed_context(self):
        """
        Guarantee embed models can be lazily loaded.
        If llama-server is running, stop it first.
        Multiple concurrent embed calls are fine.
        """
        if self._state != self.STATE_EMBED:
            try:
                await asyncio.wait_for(
                    self._switch_lock.acquire(), timeout=QUEUE_WAIT_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise HTTPException(
                    503, "Server busy — timed out waiting for embed slot (60 s)"
                )
            try:
                if self._state != self.STATE_EMBED:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._activate_embed
                    )
                    self._state = self.STATE_EMBED
            finally:
                self._switch_lock.release()

        yield   # embed models ready; caller does its work

    # ── internal activation (runs in thread executor) ──────────

    def _activate_llm(self):
        """Unload embed models → start llama-server."""
        print("🔄 ResourceManager → LLM mode")
        _unload_embed_models()
        self._start_llama_server()

    def _activate_embed(self):
        """Stop llama-server → embed models will load lazily on use."""
        print("🔄 ResourceManager → Embed mode")
        self._stop_llama_server()
        # CLIP / text engine intentionally NOT pre-loaded here;
        # they load on first actual use inside the request handler.

    # ── llama-server process management ────────────────────────

    def _start_llama_server(self):
        if self._llm_proc and self._llm_proc.poll() is None:
            print("   llama-server already running — reusing")
            return
        print("🚀 Starting llama-server …")
        cmd = [
            str(LLAMA_SERVER_BIN),
            "--model",     str(LLAMA_MODEL),
            "--host",      LLAMA_SERVER_HOST,
            "--port",      str(LLAMA_SERVER_PORT),
            "--ctx-size",  str(LLAMA_CTX),
            "--threads",   str(LLAMA_THREADS),
            "--n-predict", "256",
            "--no-mmap",   # avoids page-cache fights on 8 GB RAM
        ]
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
             kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        self._llm_proc  = subprocess.Popen(cmd, **kwargs)
        self._llm_ready = False
        print(f"   PID {self._llm_proc.pid} — waiting for /health …")
        self._wait_for_ready()

    def _wait_for_ready(self):
        """
        Poll llama-server /health with two windows:
          • 15 s fast-poll (0.5 s interval) — covers fast SSDs
          • 15 s slow-poll (1 s interval)   — covers slower eMMC / SD
        Raises RuntimeError if neither window succeeds.
        """
        for interval, window in [(0.5, 15), (1.0, 15)]:
            deadline = time.time() + window
            while time.time() < deadline:
                try:
                    response = requests.get(f"{LLAMA_SERVER_URL}/health", timeout=1)
                    if response.status_code == 200:
                        self._llm_ready = True
                        print("✅ llama-server ready")
                        return
                except requests.exceptions.ConnectionError:
                    pass
                time.sleep(interval)
        raise RuntimeError(
            "llama-server did not respond after 30 s — "
            "check LLAMA_SERVER_BIN and LLAMA_MODEL paths"
        )

    def _stop_llama_server(self):
        if not self._llm_proc:
            return
        pid = self._llm_proc.pid
        print(f"🛑 Stopping llama-server (PID {pid}) …")
        try:
            self._llm_proc.terminate()
            self._llm_proc.wait(timeout=8)
        except Exception:
            try:
                self._llm_proc.kill()
            except Exception:
                pass
        self._llm_proc  = None
        self._llm_ready = False
        gc.collect()
        print("   llama-server stopped")


# ── Embed model globals ───────────────────────────────────────
_clip_model       = None
_clip_processor   = None
_annoy_index      = None
_annoy_loaded     = False
_annoy_needs_rebuild = False
_text_engine      = None
_code_chunk_annoy_index = None
_code_chunk_annoy_loaded = False
_code_chunk_annoy_lock = threading.Lock()


def _unload_embed_models():
    global _clip_model, _clip_processor, _annoy_index, _annoy_loaded, _text_engine
    global _code_chunk_annoy_index, _code_chunk_annoy_loaded
    print("🧹 Unloading embed models …")
    if _annoy_index and hasattr(_annoy_index, "unload"):
        try:
            _annoy_index.unload()
        except Exception:
            pass
    _annoy_index    = None
    _annoy_loaded   = False
    _clip_model     = None
    _clip_processor = None
    _text_engine    = None
    _code_chunk_annoy_index = None
    _code_chunk_annoy_loaded = False
    gc.collect()
    print("   Done")


def get_text_engine():
    global _text_engine
    if _text_engine is None:
        mod = __import__(
            "text_search_implementation_v2.search", fromlist=["TextSearchEngineV2"]
        )
        _text_engine = mod.TextSearchEngineV2()
    return _text_engine


def lazy_load_clip():
    global _clip_model, _clip_processor
    if _clip_model is None:
        import os
        from transformers import CLIPProcessor, CLIPModel
        import torch
        prev_hf_offline = os.environ.get("HF_HUB_OFFLINE")
        prev_tf_offline = os.environ.get("TRANSFORMERS_OFFLINE")
        try:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            _clip_processor = CLIPProcessor.from_pretrained(
                "openai/clip-vit-base-patch32",
                local_files_only=True,
                use_fast=False,
            )
            _clip_model = CLIPModel.from_pretrained(
                "openai/clip-vit-base-patch32",
                local_files_only=True,
                use_safetensors=False,
            )
        except Exception:
            if prev_hf_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_hf_offline
            if prev_tf_offline is None:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline
            _clip_processor = CLIPProcessor.from_pretrained(
                "openai/clip-vit-base-patch32",
                use_fast=False,
            )
            _clip_model = CLIPModel.from_pretrained(
                "openai/clip-vit-base-patch32",
                use_safetensors=False,
            )
        else:
            if prev_hf_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_hf_offline
            if prev_tf_offline is None:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline
        _clip_model.to(torch.device("cpu")).eval()
    return _clip_model, _clip_processor


def embed_text_with_clip(text: str):
    model, processor = lazy_load_clip()
    import torch
    inputs = {k: v.to("cpu") for k, v in
              processor(text=[text], return_tensors="pt", padding=True).items()}
    with torch.no_grad():
        feats = model.get_text_features(**inputs)
        # Some transformer/CLIP variants may return model output objects.
        if not isinstance(feats, torch.Tensor):
            if hasattr(feats, "text_embeds") and feats.text_embeds is not None:
                feats = feats.text_embeds
            elif hasattr(feats, "pooler_output") and feats.pooler_output is not None:
                feats = feats.pooler_output
            elif hasattr(feats, "last_hidden_state") and feats.last_hidden_state is not None:
                feats = feats.last_hidden_state[:, 0, :]
            else:
                raise RuntimeError("Unsupported CLIP text feature output type")
    return (feats / feats.norm(dim=-1, keepdim=True)).squeeze(0).cpu().numpy().astype("float32")


def embed_image_file(image_path: Path):
    model, processor = lazy_load_clip()
    from PIL import Image
    import torch
    img = Image.open(image_path).convert("RGB")
    inputs = {k: v.to("cpu") for k, v in processor(images=img, return_tensors="pt").items()}
    with torch.no_grad():
        feats = model.get_image_features(**inputs)
        if not isinstance(feats, torch.Tensor):
            if hasattr(feats, "image_embeds") and feats.image_embeds is not None:
                feats = feats.image_embeds
            elif hasattr(feats, "pooler_output") and feats.pooler_output is not None:
                feats = feats.pooler_output
            elif hasattr(feats, "last_hidden_state") and feats.last_hidden_state is not None:
                feats = feats.last_hidden_state[:, 0, :]
            else:
                raise RuntimeError("Unsupported CLIP image feature output type")
    return (feats / feats.norm(dim=-1, keepdim=True)).squeeze(0).cpu().numpy().astype("float32")


def get_video_module():
    return __import__(
        "video_search_implementation_v2.video_index",
        fromlist=["scan_video_index", "search_videos"]
    )


# ── Annoy helpers ─────────────────────────────────────────────
def ensure_annoy_loaded():
    global _annoy_index, _annoy_loaded
    if _annoy_loaded:
        return True
    try:
        from annoy import AnnoyIndex
        if not ANNOY_INDEX_PATH.exists():
            _annoy_index = AnnoyIndex(ANNOY_DIM, "angular")
        else:
            ai = AnnoyIndex(ANNOY_DIM, "angular")
            ai.load(str(ANNOY_INDEX_PATH))
            _annoy_index = ai
        _annoy_loaded = True
        return True
    except Exception as e:
        print("Annoy load failed:", e)
        return False


def rebuild_annoy_index(iter_fn):
    global _annoy_index, _annoy_loaded, _annoy_needs_rebuild
    from annoy import AnnoyIndex
    with IMAGE_REBUILD_LOCK:
        print("🔧 Rebuilding Annoy index …")
        ai = AnnoyIndex(ANNOY_DIM, "angular")
        i  = 0
        for _id, vec in iter_fn():
            ai.add_item(_id, vec)
            i += 1
        if i == 0:
            _annoy_index = AnnoyIndex(ANNOY_DIM, "angular")
        else:
            ai.build(ANNOY_N_TREES)
            ai.save(str(ANNOY_INDEX_PATH))
            _annoy_index = ai
        _annoy_loaded        = True
        _annoy_needs_rebuild = False
        print(f"🔧 Annoy rebuilt: {i} items")


def all_vectors_iterator():
    for path, info in get_known_images().items():
        aid = info.get("annoy_id")
        if not aid:
            continue
        try:
            import numpy as np
            yield int(aid), np.load(str(IMAGE_EMBED_DIR / f"{aid}.npy"))
        except Exception:
            continue


# ── Image metadata DB ─────────────────────────────────────────
def _get_image_conn():
    conn = sqlite3.connect(str(IMAGE_META_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_image_meta_db():
    conn = _get_image_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE,
        filename TEXT,
        mtime REAL,
        has_ocr INTEGER DEFAULT 0,
        ocr_text TEXT DEFAULT '',
        annoy_id INTEGER,
        embedding_path TEXT,
        embedding_hash TEXT
    )""")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
        filename, ocr_text, content='images', content_rowid='id', tokenize='porter'
    )""")
    conn.commit()
    conn.close()


def get_known_images():
    conn = _get_image_conn()
    rows = conn.execute("SELECT id, path, mtime, annoy_id FROM images").fetchall()
    conn.close()
    return {r["path"]: {"id": r["id"], "mtime": r["mtime"], "annoy_id": r["annoy_id"]}
            for r in rows}


def add_or_update_image(path: str, mtime: float, annoy_id: Optional[int]):
    from pathlib import Path
    filename = Path(path).name
    conn = _get_image_conn()
    conn.execute("""INSERT INTO images (path, filename, mtime, annoy_id) VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE
        SET filename=excluded.filename, mtime=excluded.mtime, annoy_id=excluded.annoy_id""",
        (path, filename, mtime, annoy_id))
    conn.commit()
    conn.close()


def get_next_annoy_id():
    conn = _get_image_conn()
    row  = conn.execute("SELECT MAX(annoy_id) as mx FROM images").fetchone()
    conn.close()
    return int(row[0] or 0) + 1


# ── Scan helpers ──────────────────────────────────────────────
def scan_text_index(target_dir: str | None = None):
    try:
        mod = __import__(
            "text_search_implementation_v2.index_worker", fromlist=["run_scan"]
        )
        scan_dirs = [Path(target_dir).expanduser().resolve()] if target_dir else get_watch_directories()
        if hasattr(mod, "run_scan"):
            for scan_dir in scan_dirs:
                mod.run_scan(target_dir=str(scan_dir))
            return {"status": "ok", "updated": True}
        if hasattr(mod, "main"):
            mod.main()
            return {"status": "ok", "updated": True}
        return {"status": "ok", "updated": False}
    except ModuleNotFoundError:
        return {"status": "ok", "updated": False}
    except Exception:
        traceback.print_exc()
        return {"status": "error"}


def scan_cloud_text_index(remote_name: str | None = None, workers: int = 3):
    try:
        mod = __import__(
            "cloud_text_search_implementation.index_worker", fromlist=["run_scan"]
        )
        if hasattr(mod, "run_scan"):
            return mod.run_scan(remote_name=remote_name, workers=workers)
        return {"status": "ok", "updated": False}
    except ModuleNotFoundError:
        return {"status": "ok", "updated": False}
    except Exception:
        traceback.print_exc()
        return {"status": "error"}


def scan_image_index(target_dir: str | Path | None = None):
    if importlib.util.find_spec("transformers") is None:
        return {
            "status": "skipped",
            "reason": f"transformers not installed; run: {sys.executable} -m pip install --no-cache-dir torch torchvision transformers",
            "new_vectors": 0,
        }

    try:
        from image_search_implementation_v2.db import get_conn, init_db
        from image_search_implementation_v2.index_worker import IMAGE_EXTS, finalize_indexing, index_file
    except Exception as e:
        return {
            "status": "error",
            "error": f"image_v2_import_failed: {e}",
            "new_vectors": 0,
        }

    init_db()
    conn = get_conn()
    before_count = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
    conn.close()

    scan_roots = [Path(target_dir).expanduser().resolve()] if target_dir else get_watch_directories()
    scanned = 0
    failed = 0
    skip_dirs = {".venv", "venv", "__pycache__", "node_modules", ".git", ".hg", ".svn"}

    for scan_root in scan_roots:
        for p in scan_root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            if any(part in skip_dirs for part in p.parts):
                continue
            scanned += 1
            try:
                ok = bool(index_file(p))
                if not ok:
                    failed += 1
            except Exception as e:
                failed += 1
                print("image-v2 index failed:", p, e)

    conn = get_conn()
    after_count = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
    conn.close()
    new_count = max(0, after_count - before_count)
    annoy_sync = finalize_indexing(rebuild=True)

    return {
        "status": "ok",
        "engine": "annoy_sqlite_ocr",
        "new_vectors": int(new_count),
        "scanned_images": int(scanned),
        "failed_images": int(failed),
        "annoy": annoy_sync,
    }


def scan_video_index_wrapper(target_dir: str | None = None):
    try:
        combined = {"status": "ok", "new_vectors": 0}

        if target_dir:
            p = Path(target_dir).expanduser().resolve()
            if not p.is_dir():
                return {"status": "error", "error": f"Target directory not found: {p}"}
            dirs = [p]
        else:
            dirs = get_video_directories()

        for vdir in dirs:
            if not vdir.is_dir():
                continue
            result = get_video_module().scan_video_index(vdir)
            combined["new_vectors"] += result.get("new_vectors", 0)
        return combined
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def scan_audio_index_wrapper(target_dir: str | None = None):
    try:
        import sys
        if importlib.util.find_spec("faster_whisper") is None:
            return {
                "status": "skipped",
                "reason": f"faster-whisper not installed; run: {sys.executable} -m pip install --no-cache-dir faster-whisper",
            }
        # Pass audio directories via env so the worker picks them up
        env = os.environ.copy()
        if target_dir:
            p = Path(target_dir).expanduser().resolve()
            if not p.is_dir():
                return {"status": "error", "error": f"Target directory not found: {p}"}
            env["CONTEXTCORE_AUDIO_DIR"] = str(p)
        else:
            audio_dirs = get_audio_directories()
            env["CONTEXTCORE_AUDIO_DIR"] = str(audio_dirs[0]) if audio_dirs else "."

        kwargs = {"env": env}
        if platform.system() == "Windows":
             kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        subprocess.Popen([sys.executable, "-m", "audio_search_implementation_v2.worker"], **kwargs)
        return {"status": "accepted"}
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _watcher_enabled_modalities() -> dict[str, bool]:
    return {
        "text": get_enable_text(),
        "image": get_enable_image(),
        "audio": get_enable_audio(),
        "video": get_enable_video(),
        "code": get_enable_code(),
    }


def _enqueue_watch_job(modality: str, target_dir: Path) -> None:
    key = (modality, str(target_dir))
    now = time.time()
    with _watch_debounce_lock:
        last = _watch_last_job.get(key, 0.0)
        if (now - last) < 2.0:
            return
        _watch_last_job[key] = now
    _watch_queue.put({"modality": modality, "target_dir": str(target_dir)})


def _delete_text_file(path: str) -> bool:
    """Delete text file entry and its full-text search index."""
    try:
        from text_search_implementation_v2.db import delete_file_by_path_category, get_conn, _table_exists
        # Try common categories
        for category in [None, "text", "code_comment", "transcript"]:
            try:
                if category is None:
                    # Try by path without category filter
                    conn = get_conn()
                    row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
                    if row:
                        file_id = row["id"]
                        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                        try:
                            if _table_exists(conn, "files_fts_trigram"):
                                conn.execute("DELETE FROM files_fts_trigram WHERE rowid = ?", (file_id,))
                        except Exception:
                            pass
                        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
                        conn.commit()
                        conn.close()
                        return True
                    conn.close()
                else:
                    if delete_file_by_path_category(path, category):
                        return True
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"⚠️ Error deleting text file {path}: {e}")
        return False


def _delete_image_file(path: str) -> bool:
    """Delete image entry, OCR text, and embedding from Annoy index."""
    try:
        global _annoy_needs_rebuild
        from image_search_implementation_v2.db import get_conn as img_get_conn
        conn = img_get_conn()
        row = conn.execute("SELECT id, annoy_id FROM images WHERE path = ?", (path,)).fetchone()
        if row:
            image_id = int(row["id"])
            annoy_id = row["annoy_id"]

            # Delete FTS entries
            conn.execute("DELETE FROM images_fts WHERE rowid = ?", (image_id,))
            # Delete embeddings
            if annoy_id:
                emb_path = IMAGE_EMBED_DIR / f"{annoy_id}.npy"
                if emb_path.exists():
                    try:
                        emb_path.unlink()
                    except Exception:
                        pass
                _annoy_needs_rebuild = True
            # Delete image record
            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            conn.commit()
            conn.close()
            print(f"🗑️ Deleted image: {path}")
            return True
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error deleting image file {path}: {e}")
        return False


def _delete_video_file(path: str) -> bool:
    """Delete video, frames, and frame vectors from database."""
    try:
        from video_search_implementation_v2.video_index import _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT id FROM videos WHERE path = ?", (path,)).fetchone()
        if row:
            video_id = int(row["id"])
            # Get all frames and delete their vectors
            frames = conn.execute(
                "SELECT id FROM frames WHERE video_id = ?", (video_id,)
            ).fetchall()
            for frame in frames:
                try:
                    conn.execute("DELETE FROM frame_vectors WHERE rowid = ?", (frame["id"],))
                except Exception:
                    pass
                try:
                    conn.execute("DELETE FROM frames_fts WHERE rowid = ?", (frame["id"],))
                except Exception:
                    pass
            # Delete frames and video
            conn.execute("DELETE FROM frames WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
            conn.close()
            print(f"🗑️ Deleted video and {len(frames)} frames: {path}")
            return True
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error deleting video file {path}: {e}")
        return False


def _delete_audio_file(path: str) -> bool:
    """Delete audio transcript entries from text database."""
    try:
        from text_search_implementation_v2.db import delete_file_by_path_category
        # Audio transcripts are stored as text with category 'audio_transcript'
        result = delete_file_by_path_category(path, "audio_transcript")
        if result:
            print(f"🗑️ Deleted audio transcript: {path}")
        return True
    except Exception as e:
        print(f"⚠️ Error deleting audio file {path}: {e}")
        return False


def _delete_code_file(path: str) -> bool:
    """Delete code file and associated symbols/chunks."""
    try:
        conn = _code_db_conn()
        # Delete code chunks for this file
        conn.execute("DELETE FROM code_chunk_vectors WHERE chunk_id IN (SELECT id FROM code_chunks WHERE file_path = ?)", (path,))
        conn.execute("DELETE FROM code_chunks WHERE file_path = ?", (path,))
        # Delete symbols
        conn.execute("DELETE FROM code_symbols WHERE file_path = ?", (path,))
        # Delete project file entry
        conn.execute("DELETE FROM project_files WHERE file_path = ?", (path,))
        conn.commit()
        conn.close()
        global _code_chunk_annoy_needs_rebuild
        _mark_code_chunk_annoy_dirty()
        print(f"🗑️ Deleted code file and chunks: {path}")
        return True
    except Exception as e:
        print(f"⚠️ Error deleting code file {path}: {e}")
        return False


def cleanup_stale_entries() -> dict[str, int]:
    """Remove database entries for files that no longer exist on disk."""
    results = {"text_removed": 0, "image_removed": 0, "video_removed": 0, "audio_removed": 0, "code_removed": 0}

    # Clean up text files
    try:
        from text_search_implementation_v2.db import get_conn as text_get_conn
        conn = text_get_conn()
        rows = conn.execute("SELECT path FROM files").fetchall()
        for row in rows:
            path = row["path"]
            if not Path(path).exists():
                _delete_text_file(path)
                results["text_removed"] += 1
        conn.close()
    except Exception as e:
        print(f"⚠️ Error cleaning up text entries: {e}")

    # Clean up image files
    try:
        from image_search_implementation_v2.db import get_conn as img_get_conn
        conn = img_get_conn()
        rows = conn.execute("SELECT path FROM images").fetchall()
        for row in rows:
            path = row["path"]
            if not Path(path).exists():
                _delete_image_file(path)
                results["image_removed"] += 1
        conn.close()
    except Exception as e:
        print(f"⚠️ Error cleaning up image entries: {e}")

    # Clean up video files
    try:
        from video_search_implementation_v2.video_index import _get_conn as video_get_conn
        conn = video_get_conn()
        rows = conn.execute("SELECT path FROM videos").fetchall()
        for row in rows:
            path = row["path"]
            if not Path(path).exists():
                _delete_video_file(path)
                results["video_removed"] += 1
        conn.close()
    except Exception as e:
        print(f"⚠️ Error cleaning up video entries: {e}")

    # Clean up code files
    try:
        conn = _code_db_conn()
        rows = conn.execute("SELECT file_path FROM project_files").fetchall()
        for row in rows:
            path = row["file_path"]
            if not Path(path).exists():
                _delete_code_file(path)
                results["code_removed"] += 1
        conn.close()
    except Exception as e:
        print(f"⚠️ Error cleaning up code entries: {e}")

    total_removed = sum(results.values())
    print(f"🧹 Cleanup completed: removed {total_removed} stale entries")
    for modality, count in results.items():
        if count > 0:
            print(f"  {modality}: {count}")

    return results


def _route_watch_delete_event(src_path: str) -> None:
    """Route file deletion to appropriate modality handler."""
    path = Path(src_path)
    suffix = path.suffix.lower()
    enabled = _watcher_enabled_modalities()

    print(f"🗑️  Routing deletion for {src_path}, suffix: {suffix}")
    print(f"🗑️  Enabled modalities: {enabled}")

    # Text files
    if enabled["text"] and suffix in TEXT_WATCH_EXTS:
        print(f"🗑️  Deleting as text file: {src_path}")
        _delete_text_file(src_path)
    # Image files
    if enabled["image"] and suffix in IMAGE_WATCH_EXTS:
        print(f"🗑️  Deleting as image file: {src_path}")
        _delete_image_file(src_path)
    # Audio files
    if enabled["audio"] and suffix in AUDIO_WATCH_EXTS:
        print(f"🗑️  Deleting as audio file: {src_path}")
        _delete_audio_file(src_path)
    # Video files
    if enabled["video"] and suffix in VIDEO_WATCH_EXTS:
        print(f"🗑️  Deleting as video file: {src_path}")
        _delete_video_file(src_path)
    # Code files
    if enabled["code"] and (path.name in CODE_SPECIAL_FILENAMES or suffix in CODE_WATCH_EXTS):
        print(f"🗑️  Deleting as code file: {src_path}")
        _delete_code_file(src_path)

    print(f"🗑️  Deletion routing complete for {src_path}")


def _route_watch_event(src_path: str) -> None:
    path = Path(src_path)
    if not path.exists() or not path.is_file():
        return
    suffix = path.suffix.lower()
    enabled = _watcher_enabled_modalities()
    scan_dir = path.parent

    if enabled["text"] and suffix in TEXT_WATCH_EXTS:
        _enqueue_watch_job("text", scan_dir)
    if enabled["image"] and suffix in IMAGE_WATCH_EXTS:
        _enqueue_watch_job("image", scan_dir)
    if enabled["audio"] and suffix in AUDIO_WATCH_EXTS:
        _enqueue_watch_job("audio", scan_dir)
    if enabled["video"] and suffix in VIDEO_WATCH_EXTS:
        _enqueue_watch_job("video", scan_dir)
    if enabled["code"] and (path.name in CODE_SPECIAL_FILENAMES or suffix in CODE_WATCH_EXTS):
        _enqueue_watch_job("code", scan_dir)


def _content_watch_worker():
    while not _watch_stop_event.is_set():
        try:
            job = _watch_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        modality = str(job.get("modality"))
        target_dir = str(job.get("target_dir"))
        try:
            active, state = index_lock_active()
            if active:
                print(f"watcher [{modality}] skipped: full index already running from {state.get('source', 'unknown')}")
                continue
            if modality == "text":
                scan_text_index(target_dir)
            elif modality == "image":
                scan_image_index(target_dir)
            elif modality == "audio":
                scan_audio_index_wrapper(target_dir)
            elif modality == "video":
                scan_video_index_wrapper(target_dir)
            elif modality == "code":
                scan_code_index_wrapper(target_dir)
        except Exception as exc:
            print(f"watcher [{modality}] error:", exc)
        finally:
            _watch_queue.task_done()


def start_content_watcher():
    global _watch_worker_thread, _watch_observer

    _watcher_log("start_content_watcher() called")

    if _watch_worker_thread is not None and _watch_worker_thread.is_alive():
        msg = "watcher already running"
        print(msg)
        _watcher_log(msg)
        return

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        _watcher_log("watchdog imported successfully")
    except Exception as exc:
        msg = f"watcher disabled: watchdog unavailable: {exc}"
        print(msg)
        _watcher_log(msg)
        return

    class ContentEventHandler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                msg = f"Watcher: file created: {event.src_path}"
                print(f"👁️  {msg}")
                _watcher_log(msg)
                _route_watch_event(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                msg = f"Watcher: file modified: {event.src_path}"
                print(f"👁️  {msg}")
                _watcher_log(msg)
                _route_watch_event(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                msg = f"Watcher: file moved: {event.dest_path}"
                print(f"👁️  {msg}")
                _watcher_log(msg)
                _route_watch_event(event.dest_path)

        def on_deleted(self, event):
            if not event.is_directory:
                msg = f"Watcher: file deleted: {event.src_path}"
                print(f"🗑️  DELETION EVENT: {msg}")
                _watcher_log(msg)
                print(f"🗑️  Calling _route_watch_delete_event for {event.src_path}")
                _route_watch_delete_event(event.src_path)

    _watcher_log("Starting worker thread and observer")
    _watch_stop_event.clear()
    _watch_worker_thread = threading.Thread(target=_content_watch_worker, daemon=True, name="contextcore-watcher")
    _watch_worker_thread.start()
    _watcher_log("Worker thread started")

    _watch_observer = Observer()
    handler = ContentEventHandler()
    watch_dirs = get_watch_directories()
    _watcher_log(f"Watch directories: {watch_dirs}")
    started = 0
    for watch_dir in watch_dirs:
        if watch_dir.is_dir():
            _watch_observer.schedule(handler, str(watch_dir), recursive=True)
            msg = f"watching directory: {watch_dir}"
            print(msg)
            _watcher_log(msg)
            started += 1
        else:
            msg = f"watch directory not found: {watch_dir}"
            print(msg)
            _watcher_log(msg)

    if started == 0:
        msg = "watcher not started: no valid watch directories"
        print(msg)
        _watcher_log(msg)
        _watch_stop_event.set()
        if _watch_worker_thread is not None:
            _watch_worker_thread.join(timeout=2)
        _watch_worker_thread = None
        _watch_observer = None
        return

    _watch_observer.daemon = True
    _watch_observer.start()
    msg = "content watcher started successfully"
    print(msg)
    _watcher_log(msg)


def stop_content_watcher():
    global _watch_observer, _watch_worker_thread

    _watch_stop_event.set()
    if _watch_observer is not None:
        try:
            _watch_observer.stop()
            _watch_observer.join(timeout=5)
        except Exception:
            pass
        _watch_observer = None
    if _watch_worker_thread is not None:
        _watch_worker_thread.join(timeout=5)
        _watch_worker_thread = None


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
    out: list[str] = []
    for marker in FRAMEWORK_MARKERS:
        if (path / marker).exists():
            out.append(marker)
    return sorted(out)


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


def _scan_code_signals(
    root: Path,
    max_scan_files: int,
    exclude_dirs: set[str] | None = None,
    gitignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    exclude_dirs = exclude_dirs or set(COMMON_CODE_EXCLUDE_DIRS)
    gitignore_patterns = gitignore_patterns or []
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
        kept_dirs: list[str] = []
        for d in dirnames:
            child = Path(dirpath) / d
            if _should_skip_code_path(child, root, exclude_dirs, gitignore_patterns):
                generated_or_dep_dirs.add(d)
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in filenames:
            if file_count >= max_scan_files:
                break
            file_count += 1
            file_path = Path(dirpath) / fname
            if _should_skip_code_path(file_path, root, exclude_dirs, gitignore_patterns):
                continue
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

            ext = file_path.suffix.lower()
            if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".php", ".rb"}:
                code_files.append(file_path)

    for file_path in code_files[: min(len(code_files), 300)]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        snippet = text[:8000]
        matches = re.findall(r"(?:from|import|require|use|include)\s*(?:\(|from)?\s*['\"]([^'\"]+)['\"]", snippet)
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


def _load_gitignore_patterns(root: Path) -> list[str]:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns: list[str] = []
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # Skip negation rules for now; this is a strict exclude pass.
        if s.startswith("!"):
            continue
        if s.startswith("/"):
            s = s[1:]
        patterns.append(s)
    return patterns


def _matches_gitignore(rel_posix: str, name: str, patterns: list[str]) -> bool:
    for patt in patterns:
        if patt.endswith("/"):
            prefix = patt.rstrip("/")
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return True
            continue
        if "/" in patt:
            if fnmatch.fnmatch(rel_posix, patt):
                return True
        else:
            if fnmatch.fnmatch(name, patt) or fnmatch.fnmatch(rel_posix, f"*/{patt}"):
                return True
    return False


def _should_skip_code_path(path: Path, repo_root: Path, exclude_dirs: set[str], gitignore_patterns: list[str]) -> bool:
    try:
        rel_posix = str(path.resolve().relative_to(repo_root).as_posix())
    except Exception:
        rel_posix = path.name
    parts = set(Path(rel_posix).parts)
    if parts & exclude_dirs:
        return True
    if _matches_gitignore(rel_posix, path.name, gitignore_patterns):
        return True
    return False


def _categorize_top_level_dirs(repo_root: Path) -> dict[str, Any]:
    categorized = {
        "application_code": [],
        "dependency_code": [],
        "generated_code": [],
        "configuration": [],
    }
    for child in repo_root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        lname = name.lower()
        if lname in DEPENDENCY_DIR_NAMES:
            categorized["dependency_code"].append(name)
        elif lname in GENERATED_DIR_NAMES:
            categorized["generated_code"].append(name)
        elif lname.startswith("."):
            categorized["configuration"].append(name)
        else:
            categorized["application_code"].append(name)
    for category in categorized:
        categorized[category] = sorted(categorized[category])
    return {
        "directories_by_category": categorized,
        "counts": {category: len(v) for category, v in categorized.items()},
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


def analyze_code_directory(path: Path, threshold: int = 40, max_scan_files: int = 5000) -> dict[str, Any]:
    target = path.resolve()
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
    exclusion_dirs = set(COMMON_CODE_EXCLUDE_DIRS)
    for ptype in project_types:
        exclusion_dirs.update(EXCLUDES_BY_PROJECT_TYPE.get(ptype, set()))
    gitignore_patterns = _load_gitignore_patterns(project_root)
    scan_signals = _scan_code_signals(
        project_root,
        bounded_scan_limit,
        exclude_dirs=exclusion_dirs,
        gitignore_patterns=gitignore_patterns,
    )
    score, score_breakdown = _codebase_score(
        root_info=root_info,
        manifest_markers=manifest_markers,
        framework_markers=framework_markers,
        scan_signals=scan_signals,
    )
    is_code_directory = score >= bounded_threshold

    confidence_band = "low"
    if score >= 40:
        confidence_band = "high"
    elif score >= 20:
        confidence_band = "medium"
    folder_classification = _categorize_top_level_dirs(project_root)

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
        "project_classification": folder_classification,
        "score_breakdown": score_breakdown,
        "signals": scan_signals,
        "indexing_guidance": {
            "scope_rule": "Index all files under project_root except excluded directories.",
            "exclude_directories": sorted(exclusion_dirs),
            "gitignore_patterns_count": len(gitignore_patterns),
        },
    }


def scan_code_index_wrapper(path: Optional[str] = None) -> dict[str, Any]:
    try:
        roots = [Path(path).expanduser().resolve()] if path else get_code_directories()
        reports = []
        for root in roots:
            analysis = analyze_code_directory(root)
            if analysis.get("is_code_directory"):
                build = build_code_layer1_index(root)
                reports.append({"root": str(root), "analysis": analysis, "build": build})
                continue

            nested_reports = []
            for nested_root in _discover_nested_code_roots(root):
                nested_analysis = analyze_code_directory(nested_root)
                if not nested_analysis.get("is_code_directory"):
                    continue
                nested_build = build_code_layer1_index(nested_root)
                nested_reports.append(
                    {
                        "root": str(nested_root),
                        "analysis": nested_analysis,
                        "build": nested_build,
                    }
                )

            if nested_reports:
                reports.extend(nested_reports)
            else:
                reports.append(
                    {
                        "root": str(root),
                        "analysis": analysis,
                        "build": {"status": "skipped_not_code_directory", "analysis": analysis},
                    }
                )
        report_storage_dir = STORAGE_DIR / "storage"
        report_storage_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_storage_dir / "code_index_analysis_latest.json"
        report_path.write_text(json.dumps({"reports": reports}, indent=2), encoding="utf-8")
        if len(reports) == 1:
            return {"status": "ok", "analysis": reports[0]["analysis"], "build": reports[0]["build"], "report_path": str(report_path)}
        return {"status": "ok", "reports": reports, "report_path": str(report_path)}
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _discover_nested_code_roots(root: Path, max_candidates: int = 200) -> list[Path]:
    """
    Discover likely nested code project roots under a mixed-content folder.
    """
    candidates: list[Path] = []
    seen: set[str] = set()
    marker_files = {
        ".git",
        "package.json",
        "pyproject.toml",
        "setup.py",
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
    }
    code_exts = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp",
        ".cc", ".cxx", ".h", ".hpp", ".cs", ".php", ".rb", ".swift", ".kt",
    }

    for dirpath, dirnames, filenames in os.walk(root):
        dpath = Path(dirpath)

        # Do not treat the top-level mixed folder itself as a nested candidate.
        if dpath == root:
            dirnames[:] = [d for d in dirnames if d not in COMMON_CODE_EXCLUDE_DIRS]
            continue

        dirnames[:] = [d for d in dirnames if d not in COMMON_CODE_EXCLUDE_DIRS]

        has_marker = any(name in marker_files for name in filenames)
        code_file_count = sum(1 for name in filenames if Path(name).suffix.lower() in code_exts)
        if not has_marker and code_file_count < 3:
            continue

        key = str(dpath.resolve())
        if key not in seen:
            candidates.append(dpath.resolve())
            seen.add(key)
            if len(candidates) >= max_candidates:
                break

        # If current folder is a likely project root, skip descending into deeper
        # children to avoid duplicate nested roots and extra scan cost.
        dirnames[:] = []

    return candidates


def _startup_catchup_scan() -> None:
    jobs: list[tuple[str, callable]] = []
    if get_enable_text():
        jobs.append(("text", lambda: scan_text_index(None)))
    if get_enable_image() and importlib.util.find_spec("transformers") is not None:
        jobs.append(("image", lambda: scan_image_index(None)))
    elif get_enable_image():
        print(f"startup catch-up [image] skipped: install with {sys.executable} -m pip install --no-cache-dir torch torchvision transformers")
    if get_enable_audio() and importlib.util.find_spec("faster_whisper") is not None:
        jobs.append(("audio", lambda: scan_audio_index_wrapper(None)))
    elif get_enable_audio():
        print(f"startup catch-up [audio] skipped: install with {sys.executable} -m pip install --no-cache-dir faster-whisper")
    if get_enable_video():
        jobs.append(("video", lambda: scan_video_index_wrapper(None)))
    if get_enable_code():
        jobs.append(("code", lambda: scan_code_index_wrapper(None)))

    if not jobs:
        print("startup catch-up scan skipped: no enabled modalities")
        return

    targets = [str(path) for path in get_watch_directories()]
    modalities = [name for name, _ in jobs]
    acquired, state = acquire_index_lock("startup_catchup", targets, modalities)
    if not acquired:
        print(f"startup catch-up scan skipped: index already running from {state.get('source', 'unknown')}")
        return

    print("startup catch-up scan: checking configured folders for missed files")
    update_index_state(progress={"stage": "running", "current_modality": None, "completed_modalities": []})
    completed: list[str] = []
    try:
        for name, fn in jobs:
            try:
                update_index_state(progress={"stage": "running", "current_modality": name, "completed_modalities": completed})
                print(f"startup catch-up [{name}] starting")
                result = fn()
                completed.append(name)
                update_index_state(progress={"stage": "running", "current_modality": name, "completed_modalities": completed, "last_result": {name: result}})
                print(f"startup catch-up [{name}] -> {result}")
            except Exception as exc:
                update_index_state(progress={"stage": "failed", "current_modality": name, "completed_modalities": completed})
                print(f"startup catch-up [{name}] failed: {exc}")
        release_index_lock(result="completed")
    except Exception as exc:
        release_index_lock(result="failed", error=str(exc))
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code_db_conn() -> sqlite3.Connection:
    CODE_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CODE_INDEX_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_code_index_db() -> None:
    conn = _code_db_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT UNIQUE NOT NULL,
            repo_name TEXT NOT NULL,
            first_indexed_at TEXT NOT NULL,
            last_indexed_at TEXT NOT NULL,
            total_file_count INTEGER NOT NULL DEFAULT 0,
            total_line_count INTEGER NOT NULL DEFAULT 0,
            directory_stats_json TEXT
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            abs_path TEXT UNIQUE NOT NULL,
            rel_path TEXT NOT NULL,
            extension TEXT,
            language TEXT,
            line_count INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL,
            last_indexed_at TEXT NOT NULL,
            module_docstring TEXT,
            external_imports_json TEXT NOT NULL DEFAULT '[]',
            internal_imports_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(repo_id) REFERENCES repositories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_type TEXT NOT NULL,
            signature TEXT,
            docstring TEXT,
            start_line INTEGER,
            end_line INTEGER,
            is_public INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(repo_id) REFERENCES repositories(id) ON DELETE CASCADE,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);
        CREATE INDEX IF NOT EXISTS idx_files_rel ON files(repo_id, rel_path);
        CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON symbols(repo_id, symbol_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);

        CREATE TABLE IF NOT EXISTS project_structure (
            repo_path TEXT PRIMARY KEY,
            repo_name TEXT NOT NULL,
            total_files INTEGER NOT NULL DEFAULT 0,
            total_lines INTEGER NOT NULL DEFAULT 0,
            language_distribution TEXT NOT NULL DEFAULT '{}',
            directory_map TEXT NOT NULL DEFAULT '{}',
            last_scanned TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_files (
            file_path TEXT PRIMARY KEY,
            repo_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            extension TEXT,
            line_count INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            last_modified REAL NOT NULL,
            is_entry_point INTEGER NOT NULL DEFAULT 0,
            is_test_file INTEGER NOT NULL DEFAULT 0,
            is_config_file INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS code_symbols (
            symbol_id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_type TEXT NOT NULL,
            signature TEXT,
            docstring_brief TEXT,
            line_start INTEGER,
            line_end INTEGER,
            is_public INTEGER NOT NULL DEFAULT 1,
            language TEXT
        );

        CREATE TABLE IF NOT EXISTS code_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            chunk_hash TEXT NOT NULL,
            UNIQUE(repo_path, file_path, chunk_index)
        );

        CREATE TABLE IF NOT EXISTS code_chunk_vectors (
            chunk_id INTEGER PRIMARY KEY,
            vector_json TEXT NOT NULL,
            FOREIGN KEY(chunk_id) REFERENCES code_chunks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS external_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            package_name TEXT NOT NULL,
            version TEXT,
            dependency_type TEXT,
            source_file TEXT
        );

        CREATE TABLE IF NOT EXISTS internal_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            source_file TEXT NOT NULL,
            imported_file TEXT,
            import_statement TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_project_files_repo ON project_files(repo_path);
        CREATE INDEX IF NOT EXISTS idx_project_files_mtime ON project_files(repo_path, last_modified DESC);
        CREATE INDEX IF NOT EXISTS idx_code_symbols_repo_name ON code_symbols(repo_path, symbol_name);
        CREATE INDEX IF NOT EXISTS idx_code_symbols_repo_file ON code_symbols(repo_path, relative_path);
        CREATE INDEX IF NOT EXISTS idx_code_chunks_repo_file ON code_chunks(repo_path, file_path);
        CREATE INDEX IF NOT EXISTS idx_code_chunks_repo_rel ON code_chunks(repo_path, relative_path);
        CREATE INDEX IF NOT EXISTS idx_ext_deps_repo ON external_dependencies(repo_path);
        CREATE INDEX IF NOT EXISTS idx_int_deps_repo ON internal_dependencies(repo_path);
        """
    )
    conn.commit()
    conn.close()


def _language_from_path(path: Path) -> str:
    name = path.name.lower()
    ext = path.suffix.lower()
    if name == "dockerfile":
        return "dockerfile"
    if name == "makefile":
        return "make"
    if name == "cmakelists.txt":
        return "cmake"
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".scala": "scala",
        ".swift": "swift",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".sql": "sql",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
        ".ps1": "powershell",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".xml": "xml",
        ".ini": "ini",
        ".cfg": "config",
        ".conf": "config",
        ".env": "env",
        ".md": "markdown",
        ".txt": "text",
    }.get(ext, "unknown")


def _is_code_candidate(path: Path) -> bool:
    if path.name in CODE_SPECIAL_FILENAMES:
        return True
    return path.suffix.lower() in CODE_TEXT_EXTENSIONS


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return path.read_text(encoding="utf-8", errors="ignore")


def _embed_code_text(text: str) -> list[float]:
    from cloud_text_search_implementation.embeddings import embed_text

    vec = embed_text(text or "")
    return [float(x) for x in vec]


def _code_chunk_annoy_default_state() -> dict[str, Any]:
    return {"needs_rebuild": False, "last_rebuild_at": None}


def _load_code_chunk_annoy_state() -> dict[str, Any]:
    if not CODE_CHUNK_ANNOY_STATE_PATH.exists():
        return _code_chunk_annoy_default_state()
    try:
        data = json.loads(CODE_CHUNK_ANNOY_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _code_chunk_annoy_default_state()
        return {
            "needs_rebuild": bool(data.get("needs_rebuild", False)),
            "last_rebuild_at": data.get("last_rebuild_at"),
        }
    except Exception:
        return _code_chunk_annoy_default_state()


def _save_code_chunk_annoy_state(state: dict[str, Any]) -> None:
    CODE_CHUNK_ANNOY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODE_CHUNK_ANNOY_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _mark_code_chunk_annoy_dirty() -> None:
    state = _load_code_chunk_annoy_state()
    state["needs_rebuild"] = True
    _save_code_chunk_annoy_state(state)


def _clear_code_chunk_annoy_dirty() -> None:
    state = _load_code_chunk_annoy_state()
    state["needs_rebuild"] = False
    state["last_rebuild_at"] = datetime.now(timezone.utc).isoformat()
    _save_code_chunk_annoy_state(state)


def _code_chunk_annoy_installed() -> bool:
    try:
        import annoy  # noqa: F401

        return True
    except Exception:
        return False


def _upsert_code_chunks_for_file(
    conn: sqlite3.Connection,
    project_root: Path,
    abs_path: str,
    rel_path: str,
    content: str,
    chunk_lines: int = CODE_CHUNK_LINES,
    chunk_overlap: int = CODE_CHUNK_OVERLAP,
) -> int:
    repo_s = str(project_root)
    conn.execute(
        "DELETE FROM code_chunk_vectors WHERE chunk_id IN (SELECT id FROM code_chunks WHERE repo_path=? AND file_path=?)",
        (repo_s, abs_path),
    )
    conn.execute("DELETE FROM code_chunks WHERE repo_path=? AND file_path=?", (repo_s, abs_path))

    chunks = _split_code_chunks_by_lines(
        content,
        chunk_lines=max(20, int(chunk_lines)),
        chunk_overlap=max(0, int(chunk_overlap)),
    )
    inserted = 0
    for c in chunks:
        chunk_text = str(c.get("chunk", "")).strip()
        if not chunk_text:
            continue
        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8", errors="ignore")).hexdigest()
        cur = conn.execute(
            """
            INSERT INTO code_chunks(
                repo_path, file_path, relative_path, chunk_index, start_line, end_line, chunk_text, chunk_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo_s,
                abs_path,
                rel_path,
                int(c.get("chunk_index", 0)),
                int(c.get("start_line", 1)),
                int(c.get("end_line", 1)),
                chunk_text,
                chunk_hash,
            ),
        )
        chunk_id = int(cur.lastrowid)
        vec = _embed_code_text(chunk_text)
        conn.execute(
            "INSERT OR REPLACE INTO code_chunk_vectors(chunk_id, vector_json) VALUES (?, ?)",
            (chunk_id, json.dumps(vec, separators=(",", ":"))),
        )
        inserted += 1
    return inserted


def _rebuild_code_chunk_annoy_index(n_trees: int = 10) -> dict[str, Any]:
    global _code_chunk_annoy_index, _code_chunk_annoy_loaded
    with _code_chunk_annoy_lock:
        if not _code_chunk_annoy_installed():
            return {"ok": False, "error": "annoy_not_installed", "indexed_vectors": 0}

        conn = _code_db_conn()
        rows = conn.execute(
            """
            SELECT cc.id AS chunk_id, cv.vector_json
            FROM code_chunks cc
            JOIN code_chunk_vectors cv ON cv.chunk_id = cc.id
            ORDER BY cc.id ASC
            """
        ).fetchall()
        conn.close()

        parsed: list[tuple[int, list[float]]] = []
        dim = 0
        for r in rows:
            try:
                vec = json.loads(r["vector_json"] or "[]")
                if not isinstance(vec, list) or not vec:
                    continue
                fvec = [float(x) for x in vec]
                if dim == 0:
                    dim = len(fvec)
                if len(fvec) != dim:
                    continue
                parsed.append((int(r["chunk_id"]), fvec))
            except Exception:
                continue

        if dim <= 0 or not parsed:
            if CODE_CHUNK_ANNOY_PATH.exists():
                CODE_CHUNK_ANNOY_PATH.unlink(missing_ok=True)
            _code_chunk_annoy_index = None
            _code_chunk_annoy_loaded = True
            _clear_code_chunk_annoy_dirty()
            return {"ok": True, "indexed_vectors": 0, "index_exists": False, "ready": False}

        from annoy import AnnoyIndex

        ai = AnnoyIndex(dim, "angular")
        for chunk_id, vec in parsed:
            ai.add_item(int(chunk_id), vec)
        ai.build(max(2, int(n_trees)))
        CODE_CHUNK_ANNOY_PATH.parent.mkdir(parents=True, exist_ok=True)
        ai.save(str(CODE_CHUNK_ANNOY_PATH))
        _code_chunk_annoy_index = ai
        _code_chunk_annoy_loaded = True
        _clear_code_chunk_annoy_dirty()
        return {
            "ok": True,
            "indexed_vectors": int(len(parsed)),
            "index_exists": CODE_CHUNK_ANNOY_PATH.exists(),
            "ready": True,
        }


def _ensure_code_chunk_annoy_ready() -> dict[str, Any]:
    global _code_chunk_annoy_index, _code_chunk_annoy_loaded
    if not _code_chunk_annoy_installed():
        return {"ok": False, "error": "annoy_not_installed", "ready": False}

    state = _load_code_chunk_annoy_state()
    if bool(state.get("needs_rebuild")) or not CODE_CHUNK_ANNOY_PATH.exists():
        return _rebuild_code_chunk_annoy_index()

    if _code_chunk_annoy_loaded and _code_chunk_annoy_index is not None:
        return {"ok": True, "ready": True, "index_exists": True}

    conn = _code_db_conn()
    row = conn.execute(
        """
        SELECT cv.vector_json
        FROM code_chunk_vectors cv
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    if not row:
        _code_chunk_annoy_index = None
        _code_chunk_annoy_loaded = True
        return {"ok": True, "ready": False, "index_exists": False}

    try:
        vec = json.loads(row["vector_json"] or "[]")
        dim = len(vec) if isinstance(vec, list) else 0
    except Exception:
        dim = 0
    if dim <= 0:
        _code_chunk_annoy_index = None
        _code_chunk_annoy_loaded = True
        return {"ok": True, "ready": False, "index_exists": False}

    try:
        from annoy import AnnoyIndex

        ai = AnnoyIndex(dim, "angular")
        ai.load(str(CODE_CHUNK_ANNOY_PATH))
        _code_chunk_annoy_index = ai
        _code_chunk_annoy_loaded = True
        return {"ok": True, "ready": True, "index_exists": True}
    except Exception:
        return _rebuild_code_chunk_annoy_index()


def _search_code_chunk_annoy(query_vector: list[float], top_k: int = 100) -> list[dict[str, float]]:
    if not isinstance(query_vector, list) or not query_vector:
        return []
    ready = _ensure_code_chunk_annoy_ready()
    if not ready.get("ok") or not ready.get("ready"):
        return []
    if _code_chunk_annoy_index is None:
        return []
    try:
        ids, dists = _code_chunk_annoy_index.get_nns_by_vector(
            [float(x) for x in query_vector],
            max(1, int(top_k)),
            include_distances=True,
        )
    except Exception:
        return []
    out: list[dict[str, float]] = []
    for chunk_id, dist in zip(ids, dists):
        sem = max(0.0, float(1.0 - float(dist) / 2.0))
        out.append({"chunk_id": int(chunk_id), "distance": float(dist), "semantic_score": sem})
    return out


def _py_func_signature(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    parts = []
    for arg in node.args.args:
        parts.append(arg.arg)
    if node.args.vararg:
        parts.append("*" + node.args.vararg.arg)
    for arg in node.args.kwonlyargs:
        parts.append(arg.arg)
    if node.args.kwarg:
        parts.append("**" + node.args.kwarg.arg)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({', '.join(parts)})"


def _extract_python_symbols_and_imports(content: str) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    external: set[str] = set()
    internal: set[str] = set()
    module_doc = None
    try:
        tree = ast.parse(content)
        module_doc = ast.get_docstring(tree)
    except Exception:
        return {
            "module_docstring": module_doc,
            "external_imports": [],
            "internal_imports": [],
            "symbols": [],
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                external.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            target = (node.module or "").split(".")[0]
            if node.level and node.level > 0:
                if target:
                    internal.add(target)
            elif target:
                external.add(target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                {
                    "name": node.name,
                    "type": "function",
                    "signature": _py_func_signature(node),
                    "docstring": ast.get_docstring(node),
                    "start_line": int(getattr(node, "lineno", 0) or 0),
                    "end_line": int(getattr(node, "end_lineno", 0) or 0),
                    "is_public": 0 if node.name.startswith("_") else 1,
                }
            )
        elif isinstance(node, ast.ClassDef):
            base_names: list[str] = []
            for b in node.bases:
                try:
                    base_names.append(ast.unparse(b))
                except Exception:
                    pass
            signature = f"class {node.name}"
            if base_names:
                signature += f"({', '.join(base_names)})"
            symbols.append(
                {
                    "name": node.name,
                    "type": "class",
                    "signature": signature,
                    "docstring": ast.get_docstring(node),
                    "start_line": int(getattr(node, "lineno", 0) or 0),
                    "end_line": int(getattr(node, "end_lineno", 0) or 0),
                    "is_public": 0 if node.name.startswith("_") else 1,
                }
            )

    return {
        "module_docstring": module_doc,
        "external_imports": sorted(external),
        "internal_imports": sorted(internal),
        "symbols": symbols,
    }


def _extract_js_like_symbols_and_imports(content: str) -> dict[str, Any]:
    external: set[str] = set()
    internal: set[str] = set()
    symbols: list[dict[str, Any]] = []

    import_matches = re.findall(r"(?:import\s+.*?\s+from\s+|require\(|import\()\s*['\"]([^'\"]+)['\"]", content)
    for import_path in import_matches:
        if import_path.startswith(("./", "../", "/")):
            internal.add(import_path)
        else:
            external.add(import_path.split("/")[0])

    for m in re.finditer(r"^\s*export\s+class\s+([A-Za-z_]\w*)", content, flags=re.M):
        symbols.append(
            {
                "name": m.group(1),
                "type": "class",
                "signature": f"class {m.group(1)}",
                "docstring": None,
                "start_line": content.count("\n", 0, m.start()) + 1,
                "end_line": None,
                "is_public": 1,
            }
        )
    for m in re.finditer(r"^\s*(?:export\s+)?function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", content, flags=re.M):
        symbols.append(
            {
                "name": m.group(1),
                "type": "function",
                "signature": f"function {m.group(1)}({m.group(2).strip()})",
                "docstring": None,
                "start_line": content.count("\n", 0, m.start()) + 1,
                "end_line": None,
                "is_public": 0 if m.group(1).startswith("_") else 1,
            }
        )

    return {
        "module_docstring": None,
        "external_imports": sorted(external),
        "internal_imports": sorted(internal),
        "symbols": symbols,
    }


def _extract_rust_symbols_and_imports(content: str) -> dict[str, Any]:
    external: set[str] = set()
    internal: set[str] = set()
    symbols: list[dict[str, Any]] = []

    for m in re.finditer(r"^\s*use\s+([^;]+);", content, flags=re.M):
        imp = m.group(1).strip()
        if imp.startswith(("crate::", "super::", "self::")):
            internal.add(imp)
        else:
            external.add(imp.split("::")[0])

    for m in re.finditer(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", content, flags=re.M):
        name = m.group(1)
        symbols.append(
            {
                "name": name,
                "type": "function",
                "signature": f"fn {name}({m.group(2).strip()})",
                "docstring": None,
                "start_line": content.count("\n", 0, m.start()) + 1,
                "end_line": None,
                "is_public": 1 if re.search(r"^\s*pub\s+fn", m.group(0)) else 0,
            }
        )
    for m in re.finditer(r"^\s*(?:pub\s+)?(struct|trait|enum)\s+([A-Za-z_]\w*)", content, flags=re.M):
        kind = m.group(1)
        name = m.group(2)
        symbols.append(
            {
                "name": name,
                "type": "class",
                "signature": f"{kind} {name}",
                "docstring": None,
                "start_line": content.count("\n", 0, m.start()) + 1,
                "end_line": None,
                "is_public": 1 if re.search(r"^\s*pub\s+", m.group(0)) else 0,
            }
        )

    return {
        "module_docstring": None,
        "external_imports": sorted(external),
        "internal_imports": sorted(internal),
        "symbols": symbols,
    }


def _extract_code_facts(path: Path, language: str, content: str) -> dict[str, Any]:
    if language == "python":
        return _extract_python_symbols_and_imports(content)
    if language in {"javascript", "typescript"}:
        return _extract_js_like_symbols_and_imports(content)
    if language == "rust":
        return _extract_rust_symbols_and_imports(content)
    return {
        "module_docstring": None,
        "external_imports": [],
        "internal_imports": [],
        "symbols": [],
    }


def _build_directory_stats(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        rel_path = str(r["rel_path"])
        top = rel_path.split("/", 1)[0] if "/" in rel_path else "."
        ext = (r["extension"] or "").lower()
        b = buckets.setdefault(top, {"file_count": 0, "ext_counts": {}})
        b["file_count"] += 1
        b["ext_counts"][ext] = b["ext_counts"].get(ext, 0) + 1

    out: dict[str, dict[str, Any]] = {}
    for top, b in buckets.items():
        ext_counts = b["ext_counts"]
        dominant = None
        if ext_counts:
            dominant = max(ext_counts.items(), key=lambda it: it[1])[0] or "<no_ext>"
        out[top] = {"file_count": b["file_count"], "dominant_extension": dominant}
    return out


def _is_entry_point_file(rel_path: str, filename: str) -> bool:
    lname = filename.lower()
    rel = rel_path.lower()
    entry_names = {
        "main.py", "app.py", "server.py", "manage.py", "cli.py",
        "index.js", "index.ts", "main.rs", "main.go", "program.cs",
    }
    if lname in entry_names:
        return True
    return rel.startswith("cmd/") or "/cmd/" in rel or rel.startswith("bin/") or "/bin/" in rel


def _is_test_file_name(filename: str) -> bool:
    n = filename.lower()
    return n.startswith("test_") or n.endswith("_test.py") or ".test." in n or ".spec." in n


def _is_config_file(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    return path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml"}


def _doc_brief(text: str | None, max_chars: int = 100) -> str | None:
    if not text:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first[:max_chars] if first else None


def _extract_manifest_dependencies(project_root: Path) -> list[dict[str, str]]:
    deps: list[dict[str, str]] = []

    # package.json
    pkg = project_root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(_safe_read_text(pkg))
            for dep_type, dep_key in (("runtime", "dependencies"), ("dev", "devDependencies"), ("optional", "optionalDependencies")):
                items = data.get(dep_key, {})
                if isinstance(items, dict):
                    for name, version in items.items():
                        deps.append(
                            {
                                "package_name": str(name),
                                "version": str(version),
                                "dependency_type": dep_type,
                                "source_file": "package.json",
                            }
                        )
        except Exception:
            pass

    # pyproject.toml naive parse
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = _safe_read_text(pyproject)
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z0-9_.\-]+)\s*=\s*["\']([^"\']+)["\']', s)
                if m and m.group(1).lower() not in {"name", "version", "description"}:
                    deps.append(
                        {
                            "package_name": m.group(1),
                            "version": m.group(2),
                            "dependency_type": "runtime",
                            "source_file": "pyproject.toml",
                        }
                    )
        except Exception:
            pass

    # Cargo.toml naive parse
    cargo = project_root / "Cargo.toml"
    if cargo.exists():
        try:
            text = _safe_read_text(cargo)
            in_deps = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("["):
                    in_deps = s in {"[dependencies]", "[dev-dependencies]"}
                    continue
                if not in_deps or not s or s.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z0-9_.\-]+)\s*=\s*(.+)$', s)
                if m:
                    deps.append(
                        {
                            "package_name": m.group(1),
                            "version": m.group(2).strip().strip('"'),
                            "dependency_type": "runtime",
                            "source_file": "Cargo.toml",
                        }
                    )
        except Exception:
            pass

    # go.mod naive parse
    gomod = project_root / "go.mod"
    if gomod.exists():
        try:
            text = _safe_read_text(gomod)
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith("//") or s.startswith(("module ", "go ", "require (", ")")):
                    continue
                parts = s.split()
                if len(parts) >= 2:
                    deps.append(
                        {
                            "package_name": parts[0],
                            "version": parts[1],
                            "dependency_type": "runtime",
                            "source_file": "go.mod",
                        }
                    )
        except Exception:
            pass

    uniq = {}
    for d in deps:
        key = (d["package_name"], d["version"], d["dependency_type"], d["source_file"])
        uniq[key] = d
    return list(uniq.values())


def build_code_layer1_index(path: Path, force: bool = False, max_files: int = 20000) -> dict[str, Any]:
    _init_code_index_db()
    analysis = analyze_code_directory(path)
    if not analysis.get("ok"):
        return {"status": "error", "error": analysis.get("error"), "path": str(path)}
    if not analysis.get("is_code_directory"):
        return {
            "status": "skipped_not_code_directory",
            "analysis": analysis,
        }

    project_root = Path(analysis["project_root"]).resolve()
    exclude_dirs = set(analysis.get("indexing_guidance", {}).get("exclude_directories", []))
    gitignore_patterns = _load_gitignore_patterns(project_root)
    now = _now_iso()
    conn = _code_db_conn()
    indexed = 0
    skipped_unchanged = 0
    skipped_noncode = 0
    code_chunks_indexed = 0
    failed = 0
    scanned = 0
    seen_abs_paths: set[str] = set()

    row = conn.execute("SELECT id, first_indexed_at FROM repositories WHERE root_path=?", (str(project_root),)).fetchone()
    if row:
        repo_id = int(row["id"])
        first_indexed_at = str(row["first_indexed_at"])
    else:
        conn.execute(
            """
            INSERT INTO repositories(root_path, repo_name, first_indexed_at, last_indexed_at, total_file_count, total_line_count, directory_stats_json)
            VALUES (?, ?, ?, ?, 0, 0, '{}')
            """,
            (str(project_root), project_root.name, now, now),
        )
        repo_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        first_indexed_at = now

    # Layer 3 reset for this repo before rebuild pass.
    conn.execute("DELETE FROM external_dependencies WHERE repo_path=?", (str(project_root),))
    conn.execute("DELETE FROM internal_dependencies WHERE repo_path=?", (str(project_root),))

    manifest_deps = _extract_manifest_dependencies(project_root)
    for dep in manifest_deps:
        conn.execute(
            """
            INSERT INTO external_dependencies(repo_path, package_name, version, dependency_type, source_file)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(project_root),
                dep.get("package_name"),
                dep.get("version"),
                dep.get("dependency_type"),
                dep.get("source_file"),
            ),
        )

    for dirpath, dirnames, filenames in os.walk(project_root):
        kept_dirs: list[str] = []
        for d in dirnames:
            child = Path(dirpath) / d
            if _should_skip_code_path(child, project_root, exclude_dirs, gitignore_patterns):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for fname in filenames:
            if scanned >= max_files:
                break
            scanned += 1
            file_path = Path(dirpath) / fname
            if _should_skip_code_path(file_path, project_root, exclude_dirs, gitignore_patterns):
                skipped_noncode += 1
                continue
            if not _is_code_candidate(file_path):
                skipped_noncode += 1
                continue
            if should_ignore(file_path):
                skipped_noncode += 1
                continue

            try:
                stat = file_path.stat()
                if stat.st_size > CODE_INDEX_MAX_FILE_SIZE_BYTES:
                    skipped_noncode += 1
                    continue
                mtime = float(stat.st_mtime)
                abs_path = str(file_path.resolve())
                rel_path = str(file_path.resolve().relative_to(project_root)).replace("\\", "/")
                ext = file_path.suffix.lower()
                lang = _language_from_path(file_path)
                seen_abs_paths.add(abs_path)

                existing = conn.execute(
                    "SELECT id, mtime FROM files WHERE abs_path=?",
                    (abs_path,),
                ).fetchone()
                if existing and (not force) and abs(float(existing["mtime"]) - mtime) < 1e-6:
                    has_pf = conn.execute(
                        "SELECT 1 FROM project_files WHERE file_path=? LIMIT 1",
                        (abs_path,),
                    ).fetchone()
                    has_cs = conn.execute(
                        "SELECT 1 FROM code_symbols WHERE file_path=? LIMIT 1",
                        (abs_path,),
                    ).fetchone()
                    has_chunks = conn.execute(
                        "SELECT 1 FROM code_chunks WHERE repo_path=? AND file_path=? LIMIT 1",
                        (str(project_root), abs_path),
                    ).fetchone()
                    if has_pf and has_cs and has_chunks:
                        skipped_unchanged += 1
                        continue

                content = _safe_read_text(file_path)
                line_count = content.count("\n") + (1 if content else 0)
                facts = _extract_code_facts(file_path, lang, content)

                conn.execute(
                    """
                    INSERT INTO files(
                        repo_id, abs_path, rel_path, extension, language, line_count, mtime,
                        last_indexed_at, module_docstring, external_imports_json, internal_imports_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(abs_path) DO UPDATE SET
                        repo_id=excluded.repo_id,
                        rel_path=excluded.rel_path,
                        extension=excluded.extension,
                        language=excluded.language,
                        line_count=excluded.line_count,
                        mtime=excluded.mtime,
                        last_indexed_at=excluded.last_indexed_at,
                        module_docstring=excluded.module_docstring,
                        external_imports_json=excluded.external_imports_json,
                        internal_imports_json=excluded.internal_imports_json
                    """,
                    (
                        repo_id,
                        abs_path,
                        rel_path,
                        ext,
                        lang,
                        line_count,
                        mtime,
                        now,
                        facts.get("module_docstring"),
                        json.dumps(facts.get("external_imports", [])),
                        json.dumps(facts.get("internal_imports", [])),
                    ),
                )
                file_row = conn.execute("SELECT id FROM files WHERE abs_path=?", (abs_path,)).fetchone()
                if not file_row:
                    failed += 1
                    continue
                file_id = int(file_row["id"])
                conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
                conn.execute("DELETE FROM code_symbols WHERE repo_path=? AND file_path=?", (str(project_root), abs_path))
                conn.execute("DELETE FROM internal_dependencies WHERE repo_path=? AND source_file=?", (str(project_root), rel_path))
                for s in facts.get("symbols", []):
                    conn.execute(
                        """
                        INSERT INTO symbols(
                            repo_id, file_id, symbol_name, symbol_type, signature, docstring,
                            start_line, end_line, is_public
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repo_id,
                            file_id,
                            s.get("name"),
                            s.get("type"),
                            s.get("signature"),
                            s.get("docstring"),
                            s.get("start_line"),
                            s.get("end_line"),
                            int(s.get("is_public", 1)),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO code_symbols(
                            repo_path, file_path, relative_path, symbol_name, symbol_type, signature,
                            docstring_brief, line_start, line_end, is_public, language
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(project_root),
                            abs_path,
                            rel_path,
                            s.get("name"),
                            s.get("type"),
                            s.get("signature"),
                            _doc_brief(s.get("docstring")),
                            s.get("start_line"),
                            s.get("end_line"),
                            int(s.get("is_public", 1)),
                            lang,
                        ),
                    )

                # Layer 1 normalized file row.
                conn.execute(
                    """
                    INSERT INTO project_files(
                        file_path, repo_path, relative_path, extension, line_count, size_bytes, last_modified,
                        is_entry_point, is_test_file, is_config_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        repo_path=excluded.repo_path,
                        relative_path=excluded.relative_path,
                        extension=excluded.extension,
                        line_count=excluded.line_count,
                        size_bytes=excluded.size_bytes,
                        last_modified=excluded.last_modified,
                        is_entry_point=excluded.is_entry_point,
                        is_test_file=excluded.is_test_file,
                        is_config_file=excluded.is_config_file
                    """,
                    (
                        abs_path,
                        str(project_root),
                        rel_path,
                        ext,
                        line_count,
                        int(stat.st_size),
                        mtime,
                        1 if _is_entry_point_file(rel_path, fname) else 0,
                        1 if _is_test_file_name(fname) else 0,
                        1 if _is_config_file(file_path) else 0,
                    ),
                )

                code_chunks_indexed += _upsert_code_chunks_for_file(
                    conn=conn,
                    project_root=project_root,
                    abs_path=abs_path,
                    rel_path=rel_path,
                    content=content,
                    chunk_lines=CODE_CHUNK_LINES,
                    chunk_overlap=CODE_CHUNK_OVERLAP,
                )

                for internal in facts.get("internal_imports", []):
                    conn.execute(
                        """
                        INSERT INTO internal_dependencies(repo_path, source_file, imported_file, import_statement)
                        VALUES (?, ?, ?, ?)
                        """,
                        (str(project_root), rel_path, None, str(internal)),
                    )
                indexed += 1
            except Exception:
                failed += 1
                traceback.print_exc()
        if scanned >= max_files:
            break

    repo_files = conn.execute(
        "SELECT rel_path, extension, line_count, language FROM files WHERE repo_id=?",
        (repo_id,),
    ).fetchall()

    # Remove stale rows for deleted files.
    stale_rows = conn.execute("SELECT abs_path FROM files WHERE repo_id=?", (repo_id,)).fetchall()
    stale_abs = [str(r["abs_path"]) for r in stale_rows if str(r["abs_path"]) not in seen_abs_paths]
    for abs_path in stale_abs:
        conn.execute(
            "DELETE FROM code_chunk_vectors WHERE chunk_id IN (SELECT id FROM code_chunks WHERE repo_path=? AND file_path=?)",
            (str(project_root), abs_path),
        )
        conn.execute("DELETE FROM code_chunks WHERE repo_path=? AND file_path=?", (str(project_root), abs_path))
        conn.execute("DELETE FROM files WHERE abs_path=?", (abs_path,))
        conn.execute("DELETE FROM project_files WHERE file_path=?", (abs_path,))
        conn.execute("DELETE FROM code_symbols WHERE file_path=?", (abs_path,))

    # Refresh repo files snapshot after stale cleanup.
    repo_files = conn.execute(
        "SELECT rel_path, extension, line_count, language FROM files WHERE repo_id=?",
        (repo_id,),
    ).fetchall()
    total_file_count = len(repo_files)
    total_line_count = sum(int(r["line_count"] or 0) for r in repo_files)
    dir_stats = _build_directory_stats(repo_files)

    conn.execute(
        """
        UPDATE repositories
        SET last_indexed_at=?, total_file_count=?, total_line_count=?, directory_stats_json=?
        WHERE id=?
        """,
        (now, total_file_count, total_line_count, json.dumps(dir_stats), repo_id),
    )

    # Keep Layer 1 aggregate table synchronized.
    lang_dist: dict[str, int] = {}
    for r in repo_files:
        lang = str(r["language"] or "unknown")
        lang_dist[lang] = lang_dist.get(lang, 0) + 1
    conn.execute(
        """
        INSERT INTO project_structure(
            repo_path, repo_name, total_files, total_lines, language_distribution, directory_map, last_scanned
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_path) DO UPDATE SET
            repo_name=excluded.repo_name,
            total_files=excluded.total_files,
            total_lines=excluded.total_lines,
            language_distribution=excluded.language_distribution,
            directory_map=excluded.directory_map,
            last_scanned=excluded.last_scanned
        """,
        (
            str(project_root),
            project_root.name,
            total_file_count,
            total_line_count,
            json.dumps(lang_dist),
            json.dumps(dir_stats),
            now,
        ),
    )
    if code_chunks_indexed > 0 or stale_abs:
        _mark_code_chunk_annoy_dirty()
    conn.commit()
    conn.close()

    return {
        "status": "ok",
        "repo_id": repo_id,
        "repo_root": str(project_root),
        "repo_name": project_root.name,
        "first_indexed_at": first_indexed_at,
        "last_indexed_at": now,
        "scanned_files": scanned,
        "indexed_files": indexed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_noncode_or_ignored": skipped_noncode,
        "failed_files": failed,
        "code_chunks_indexed": int(code_chunks_indexed),
        "totals": {
            "file_count": total_file_count,
            "line_count": total_line_count,
        },
        "db_path": str(CODE_INDEX_DB),
    }


# ── Search helpers ────────────────────────────────────────────
def run_text_search(
    query: str,
    top_k: int = 20,
    include_metadata: bool = False,
    chunk_chars: int = 900,
    chunk_overlap: int = 120,
    exclude_sources: set[str] | None = None,
    retrieval_mode: str = "contextcore_hybrid",
    max_context_tokens_per_result: int | None = None,
    max_chunks_per_doc: int = 1,
):
    try:
        local_results = get_text_engine().search(
            query=query,
            categories=None,
            top_k=top_k,
            include_metadata=include_metadata,
            chunk_chars=chunk_chars,
            chunk_overlap=chunk_overlap,
            exclude_sources=exclude_sources,
            retrieval_mode=retrieval_mode,
            max_context_tokens_per_result=max_context_tokens_per_result,
            max_chunks_per_doc=max_chunks_per_doc,
        )
        try:
            from cloud_text_search_implementation.search import search_cloud_text

            cloud_results = search_cloud_text(
                query=query,
                top_k=top_k,
                exclude_sources=exclude_sources,
            )
        except Exception:
            cloud_results = []

        merged = []
        if isinstance(local_results, list):
            merged.extend(local_results)
        if isinstance(cloud_results, list):
            merged.extend(cloud_results)

        merged.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return merged[:top_k]
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def _run_image_search_legacy(query: str, top_k: int = 10):
    try:
        init_image_meta_db()
        if not ensure_annoy_loaded():
            return {"error": "annoy_unavailable"}
        conn = _get_image_conn()
        cnt  = conn.execute(
            "SELECT COUNT(*) as c FROM images WHERE annoy_id IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        if cnt > 0 and (not ANNOY_INDEX_PATH.exists() or _annoy_needs_rebuild):
            rebuild_annoy_index(all_vectors_iterator)
        qvec = embed_text_with_clip(query)
        if _annoy_index is None:
            return {"error": "annoy_not_loaded"}
        ids, dists = _annoy_index.get_nns_by_vector(
            qvec.tolist(), top_k, include_distances=True
        )
        conn    = _get_image_conn()
        results = []
        for aid, dist in zip(ids, dists):
            row = conn.execute(
                "SELECT path FROM images WHERE annoy_id=?", (aid,)
            ).fetchone()
            if row:
                results.append({
                    "path":     row[0],
                    "annoy_id": aid,
                    "score":    float(1.0 - dist / 2.0),
                })
        conn.close()
        return {"hits": results[:max(1, int(top_k))]}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def _image_v2_capabilities() -> dict[str, Any]:
    ocr_pkg = importlib.util.find_spec("pytesseract") is not None
    ocr_bin = shutil.which("tesseract") is not None

    annoy_installed = False
    annoy_ready = False
    annoy_needs_rebuild = False
    try:
        from image_search_implementation_v2.annoy_store import get_annoy_status

        annoy_status = get_annoy_status()
        annoy_installed = bool(annoy_status.get("installed"))
        annoy_ready = bool(annoy_status.get("ready"))
        annoy_needs_rebuild = bool(annoy_status.get("needs_rebuild"))
    except Exception:
        pass

    return {
        "ocr_available": bool(ocr_pkg and ocr_bin),
        "ocr_package_installed": bool(ocr_pkg),
        "ocr_binary_installed": bool(ocr_bin),
        "annoy_installed": annoy_installed,
        "annoy_ready": annoy_ready,
        "annoy_needs_rebuild": annoy_needs_rebuild,
        "semantic_backend_available": bool(annoy_ready),
        "semantic_backend": "annoy_sqlite",
    }


def run_image_search(query: str, top_k: int = 10):
    bounded_top_k = max(1, min(int(top_k), 50))

    try:
        from image_search_implementation_v2.db import init_db
        from image_search_implementation_v2.search import search as image_search_v2

        init_db()
        hits = image_search_v2(query, top_k=bounded_top_k)
        if not isinstance(hits, list):
            hits = []
        return {
            "hits": hits[:bounded_top_k],
            "engine": "annoy_sqlite_ocr",
            "capabilities": _image_v2_capabilities(),
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "error": "image_v2_unavailable",
            "detail": str(e),
            "capabilities": _image_v2_capabilities(),
        }


def run_video_search(query: str, top_k: int = 10):
    try:
        return get_video_module().search_videos(query, top_k)
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def run_audio_search(query: str, top_k: int = 10):
    try:
        from text_search_implementation_v2.db import query_fts, get_file_metadata_by_ids, get_fts_content_by_ids

        tokens = query.strip().lower().split()
        if tokens:
            match_q = " OR ".join(t + "*" for t in tokens)
            rows = query_fts(match_q, limit=top_k * 2)
        else:
            rows = []

        audio_results = []
        for r in rows:
            meta = get_file_metadata_by_ids([r["id"]]).get(r["id"])
            if not meta or meta.get("category") != "audio":
                continue

            content = get_fts_content_by_ids([r["id"]]).get(r["id"], "")
            audio_results.append({
                "path": meta["path"],
                "filename": meta["filename"],
                "category": meta["category"],
                "score": -float(r["score"]),
                "transcript": content[:500] if content else "",
            })
            if len(audio_results) >= top_k:
                break

        return {"hits": audio_results}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  FastAPI app
# ═══════════════════════════════════════════════════════════════
app = FastAPI(title="Unified Search — memory-managed")
rm  = ResourceManager()


@app.on_event("startup")
async def startup():
    init_image_meta_db()
    print("🚀 unimain started (no idle-unload — models persist until switched)")
    network_bootstrap()
    auto_prewarm = os.getenv("CONTEXTCORE_PREWARM_ON_STARTUP", "1").strip().lower() not in {"0", "false", "no"}
    if auto_prewarm:
        print("🔥 startup prewarm enabled")
        try:
            get_text_engine()
        except Exception as e:
            print("⚠️ text prewarm failed:", e)
        if get_enable_image() or get_enable_video():
            if importlib.util.find_spec("transformers") is None:
                print("⚠️ CLIP prewarm skipped: transformers not installed")
                print(f"   Install with: {sys.executable} -m pip install --no-cache-dir torch torchvision transformers")
            else:
                try:
                    lazy_load_clip()
                    print("✅ CLIP prewarmed")
                except Exception as e:
                    print("⚠️ CLIP prewarm failed:", e)
                    print(f"   Retry with: {sys.executable} -m pip install --no-cache-dir torch torchvision transformers")

    if os.getenv("CONTEXTCORE_ENABLE_WATCHER", "1").strip().lower() not in {"0", "false", "no"}:
        try:
            print("🔍 Starting content watcher...")
            start_content_watcher()
            print(f"✅ Watcher started: observer={_watch_observer}, worker={_watch_worker_thread}")
        except Exception as e:
            print(f"⚠️ watcher startup failed: {e}")
            import traceback
            traceback.print_exc()

    if os.getenv("CONTEXTCORE_STARTUP_SCAN", "1").strip().lower() not in {"0", "false", "no"}:
        threading.Thread(target=_startup_catchup_scan, daemon=True, name="contextcore-startup-scan").start()


@app.on_event("shutdown")
async def shutdown():
    rm._stop_llama_server()
    _unload_embed_models()
    stop_content_watcher()


# ── Health ────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status":         "ok",
        "resource_state": rm._state,
        "llm_running":    rm._llm_proc is not None and rm._llm_proc.poll() is None,
    }


# ── /llm ─────────────────────────────────────────────────────
@app.post("/llm")
async def run_llm(
    query:       str   = Body(..., embed=True),
    max_tokens:  int   = Body(256, embed=True),
    temperature: float = Body(0.7, embed=True),
):
    """
    LLM inference via llama-server.

    On first call (or after a switch from embed mode):
      embed models unloaded → llama-server spawned → weights loaded once.
    On subsequent calls while in LLM mode:
      llama-server already running → just proxy the request, no reload.
    Any concurrent /search call that arrives while this is running:
      queued → waits up to 60 s → then served (llama-server is stopped first).
    """
    if not query.strip():
        raise HTTPException(400, "Empty query")

    current_temp = get_cpu_temp_c()
    if current_temp >= THERMAL_LIMIT_C:
        raise HTTPException(
            status_code=503,
            detail={
                "error":           "thermal_throttle",
                "message":         f"CPU too hot ({current_temp:.1f}°C / limit {THERMAL_LIMIT_C}°C). Please try again shortly.",
                "current_temp_c":  current_temp,
                "limit_temp_c":    THERMAL_LIMIT_C,
            },
            headers={"Retry-After": "30"},
        )

    async with rm.llm_context():
        payload = {
            "prompt":      query,
            "n_predict":   max_tokens,
            "temperature": temperature,
            "stop":        ["</s>", "<|im_end|>"],
            "stream":      False,
        }
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.post(
                    f"{LLAMA_SERVER_URL}/completion",
                    json=payload,
                    timeout=120,
                ),
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "response":         data.get("content", ""),
                "tokens_predicted": data.get("tokens_predicted"),
                "stop_reason":      data.get("stop_type"),
            }
        except requests.exceptions.Timeout:
            raise HTTPException(504, "LLM inference timed out")
        except Exception as e:
            raise HTTPException(500, str(e))


# ── /search ───────────────────────────────────────────────────
@app.get("/search")
async def unified_search(
    query: str = Query(..., min_length=1),
    top_k: int = Query(20, ge=1, le=200),
    modality: str = Query("all"),
    text_include_metadata: bool = Query(False),
    text_chunk_chars: int = Query(900, ge=200, le=4000),
    text_chunk_overlap: int = Query(120, ge=0, le=1000),
    text_retrieval_mode: str = Query("contextcore_hybrid"),
    text_max_context_tokens_per_result: int | None = Query(None, ge=1, le=4000),
    text_max_chunks_per_doc: int = Query(1, ge=1, le=4),
    exclude_sources: str | None = Query(None),
):
    """
    Unified text + image + video search.

    On first call (or after LLM mode): llama-server stopped → embed models
    load lazily. On subsequent calls while in embed mode: models already
    warm, no reload overhead.
    """
    q = query.strip()
    if not q:
        raise HTTPException(400, "empty query")

    mode = modality.strip().lower()
    if mode not in {"all", "text", "image", "video", "audio"}:
        raise HTTPException(400, "invalid modality; expected all,text,image,video,audio")

    excluded: set[str] = set()
    if exclude_sources:
        excluded = {p.strip() for p in exclude_sources.split(",") if p.strip()}

    async with rm.embed_context():
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=SEARCH_THREADPOOL) as ex:
            text_res = image_res = video_res = audio_res = None
            if mode in {"all", "text"}:
                try:
                    text_res  = await asyncio.wait_for(
                        loop.run_in_executor(
                            ex,
                            run_text_search,
                            q,
                            top_k,
                            text_include_metadata,
                            text_chunk_chars,
                            text_chunk_overlap,
                            excluded,
                            text_retrieval_mode,
                            text_max_context_tokens_per_result,
                            text_max_chunks_per_doc,
                        ),
                        timeout=8,
                    )
                except Exception as e:
                    text_res  = {"error": str(e)}
            if mode in {"all", "audio"}:
                try:
                    audio_res = await asyncio.wait_for(
                        loop.run_in_executor(ex, run_audio_search, q, min(50, top_k)), timeout=8)
                except Exception as e:
                    audio_res = {"error": str(e)}
            if mode in {"all", "image"}:
                try:
                    image_res = await asyncio.wait_for(
                        loop.run_in_executor(ex, run_image_search, q, min(50, top_k)), timeout=10)
                except Exception as e:
                    image_res = {"error": str(e)}
            if mode in {"all", "video"}:
                try:
                    video_res = await asyncio.wait_for(
                        loop.run_in_executor(ex, run_video_search, q, min(50, top_k)), timeout=12)
                except Exception as e:
                    video_res = {"error": str(e)}

    out = {"query": q, "modality": mode}
    text_results = text_res if isinstance(text_res, list) else []
    text_results = [t for t in text_results if t.get("category") != "audio"]
    out["text"]  = ({"count": len(text_results),  "results": text_results}
                    if isinstance(text_res, list)
                    else {"count": 0, "results": [],
                          "error": text_res.get("error") if isinstance(text_res, dict) else None})
    out["audio"] = ({"count": len(audio_res.get("hits", [])), "results": audio_res.get("hits", [])}
                    if isinstance(audio_res, dict)
                    else {"count": 0, "results": [],
                          "error": audio_res.get("error") if isinstance(audio_res, dict) else None})
    out["image"] = ({"count": len(image_res["hits"]), "results": image_res["hits"]}
                    if isinstance(image_res, dict) and "hits" in image_res
                    else {"count": 0, "results": [],
                          "error": image_res.get("error") if isinstance(image_res, dict) else None})
    out["video"] = ({"count": len(video_res["hits"]), "results": video_res["hits"]}
                    if isinstance(video_res, dict) and "hits" in video_res
                    else {"count": 0, "results": [],
                          "error": video_res.get("error") if isinstance(video_res, dict) else None})

    hit_paths: list[str] = []
    for section in ("text", "audio", "image", "video"):
        items = out.get(section, {}).get("results", [])
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, dict):
                continue
            candidate = row.get("path") or row.get("video_path") or row.get("source")
            if isinstance(candidate, str) and candidate.strip():
                hit_paths.append(candidate.strip())
    try:
        record_search_hits(hit_paths)
    except Exception:
        pass
    return out


@app.get("/search/text/neighbors")
def text_neighbors(
    chunk_id: str = Query(..., min_length=8),
    direction: str = Query("next"),
    count: int = Query(1, ge=1, le=5),
):
    try:
        result = get_text_engine().get_neighbors(
            chunk_id=chunk_id,
            direction=direction,
            count=count,
        )
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── /index/scan ───────────────────────────────────────────────
@app.post("/index/scan")
async def index_scan(
    run_text:  bool = True,
    run_image: bool = True,
    run_video: bool = True,
    run_audio: bool = True,
    run_code:  bool = False,
    code_path: str | None = None,
    target_dir: str | None = None,
):
    """Fire-and-forget scan. Acquires embed context so it queues
    correctly behind any active LLM call."""
    if run_code and code_path:
        code_root = Path(code_path).expanduser().resolve()
        if not code_root.exists() or not code_root.is_dir():
            raise HTTPException(404, "code_path directory not found")
        if not _is_path_allowed(code_root):
            raise HTTPException(403, "code_path not allowed")

    if target_dir:
        t_root = Path(target_dir).expanduser().resolve()
        if not t_root.exists() or not t_root.is_dir():
            raise HTTPException(404, "target_dir not found")
        if not _is_path_allowed(t_root):
            raise HTTPException(403, "target_dir not allowed")
    submitted = [n for n, f in [
        ("text", run_text), ("image", run_image),
        ("video", run_video), ("audio", run_audio),
        ("code", run_code),
    ] if f]
    targets = [target_dir] if target_dir else [str(path) for path in get_watch_directories()]
    acquired, state = acquire_index_lock("manual_scan", targets, submitted)
    if not acquired:
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "jobs": submitted, "target": target_dir if target_dir else "global", "state": state},
        )

    async def _do_scans():
        try:
            async with rm.embed_context():
                loop = asyncio.get_event_loop()
                pool = ThreadPoolExecutor(max_workers=SCAN_THREADPOOL)
                jobs = []
                completed: list[str] = []

                update_index_state(progress={"stage": "running", "current_modality": None, "completed_modalities": completed})

                if run_text:
                    jobs.append(("text", loop.run_in_executor(pool, scan_text_index, target_dir)))
                if run_image:
                    jobs.append(("image", loop.run_in_executor(pool, scan_image_index, target_dir)))
                if run_video:
                    jobs.append(("video", loop.run_in_executor(pool, scan_video_index_wrapper, target_dir)))
                if run_audio:
                    jobs.append(("audio", loop.run_in_executor(pool, scan_audio_index_wrapper, target_dir)))
                if run_code:
                    jobs.append(("code", loop.run_in_executor(pool, lambda: scan_code_index_wrapper(code_path))))

                for name, fut in jobs:
                    try:
                        update_index_state(progress={"stage": "running", "current_modality": name, "completed_modalities": completed})
                        result = await fut
                        completed.append(name)
                        update_index_state(progress={"stage": "running", "current_modality": name, "completed_modalities": completed, "last_result": {name: result}})
                        print(f"scan [{name}]:", result)
                    except Exception as e:
                        update_index_state(progress={"stage": "failed", "current_modality": name, "completed_modalities": completed})
                        print(f"scan [{name}] error:", e)
                release_index_lock(result="completed")
                return
        except Exception as e:
            release_index_lock(result="failed", error=str(e))
            raise
        async with rm.embed_context():
            loop = asyncio.get_event_loop()
            pool = ThreadPoolExecutor(max_workers=SCAN_THREADPOOL)
            jobs = []

            # Always call scan helpers directly — they handle target_dir=None as "use config"
            if run_text:  jobs.append(("text",  loop.run_in_executor(pool, scan_text_index,          target_dir)))
            if run_image: jobs.append(("image", loop.run_in_executor(pool, scan_image_index,         target_dir)))
            if run_video: jobs.append(("video", loop.run_in_executor(pool, scan_video_index_wrapper, target_dir)))
            if run_audio: jobs.append(("audio", loop.run_in_executor(pool, scan_audio_index_wrapper, target_dir)))
            if run_code:  jobs.append(("code",  loop.run_in_executor(pool, lambda: scan_code_index_wrapper(code_path))))

            for name, fut in jobs:
                try:
                    print(f"scan [{name}]:", await fut)
                except Exception as e:
                    print(f"scan [{name}] error:", e)

    asyncio.ensure_future(_do_scans())
    submitted = [n for n, f in [
        ("text", run_text), ("image", run_image),
        ("video", run_video), ("audio", run_audio),
        ("code", run_code),
    ] if f]
    return {"status": "accepted", "jobs": submitted, "target": target_dir if target_dir else "global"}


@app.post("/index/cloud/scan")
async def index_cloud_scan(
    remote_name: str | None = None,
    workers: int = Query(3, ge=1, le=8),
):
    selected_remote = (remote_name or "").strip() or None
    lock_target = selected_remote or "configured_remote"
    acquired, state = acquire_index_lock("cloud_scan", [lock_target], ["cloud_text"])
    if not acquired:
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "jobs": ["cloud_text"], "target": lock_target, "state": state},
        )

    async def _do_cloud_scan():
        try:
            update_index_state(progress={"stage": "running", "current_modality": "cloud_text", "completed_modalities": []})
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: scan_cloud_text_index(remote_name=selected_remote, workers=workers),
            )
            update_index_state(
                progress={
                    "stage": "completed",
                    "current_modality": "cloud_text",
                    "completed_modalities": ["cloud_text"],
                    "last_result": {"cloud_text": result},
                }
            )
            release_index_lock(result="completed")
            return
        except Exception as e:
            release_index_lock(result="failed", error=str(e))
            raise

    asyncio.ensure_future(_do_cloud_scan())
    return {"status": "accepted", "jobs": ["cloud_text"], "target": lock_target}


@app.post("/index/code/analyze")
def index_code_analyze(
    path: str = Query("."),
    threshold: int = Query(40, ge=0, le=100),
    max_scan_files: int = Query(5000, ge=100, le=20000),
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    result = analyze_code_directory(root, threshold=threshold, max_scan_files=max_scan_files)
    report_storage_dir = STORAGE_DIR / "storage"
    report_storage_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_storage_dir / "code_index_analysis_latest.json"
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_path"] = str(report_path)
    return result


@app.post("/index/code/layer1/build")
def index_code_layer1_build(
    path: str = Query("."),
    force: bool = Query(False),
    max_files: int = Query(20000, ge=100, le=500000),
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")
    return build_code_layer1_index(root, force=force, max_files=max_files)


def _resolve_repo_id_for_path(conn: sqlite3.Connection, path: Path) -> tuple[int, Path]:
    analysis = analyze_code_directory(path)
    if not analysis.get("ok"):
        raise HTTPException(400, analysis.get("error", "Unable to analyze directory"))
    repo_root = Path(str(analysis["project_root"])).resolve()
    row = conn.execute("SELECT id FROM repositories WHERE root_path=?", (str(repo_root),)).fetchone()
    if not row:
        raise HTTPException(404, "Repository not indexed in Layer 1 DB")
    return int(row["id"]), repo_root


def _fetch_layer1_payload(
    conn: sqlite3.Connection,
    repo_id: int,
    repo_root: Path,
    include_all: bool,
    files_limit: int,
    symbols_limit: int,
) -> dict[str, Any]:
    repo = conn.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
    if not repo:
        raise HTTPException(404, "Repository not indexed in Layer 1 DB")

    lang_rows = conn.execute(
        "SELECT language, COUNT(*) AS c FROM files WHERE repo_id=? GROUP BY language ORDER BY c DESC",
        (repo_id,),
    ).fetchall()
    symbol_count = int(conn.execute("SELECT COUNT(*) FROM symbols WHERE repo_id=?", (repo_id,)).fetchone()[0])

    files_query_limit = 1_000_000 if include_all else files_limit
    symbols_query_limit = 1_000_000 if include_all else symbols_limit

    file_rows = conn.execute(
        """
        SELECT id, rel_path, extension, language, line_count, mtime, last_indexed_at, module_docstring,
               external_imports_json, internal_imports_json
        FROM files
        WHERE repo_id=?
        ORDER BY rel_path
        LIMIT ?
        """,
        (repo_id, files_query_limit),
    ).fetchall()
    symbol_rows = conn.execute(
        """
        SELECT s.symbol_name, s.symbol_type, s.signature, s.docstring, s.start_line, s.end_line,
               s.is_public, f.rel_path
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.repo_id=?
        ORDER BY s.symbol_name, f.rel_path
        LIMIT ?
        """,
        (repo_id, symbols_query_limit),
    ).fetchall()

    files = [
        {
            "file_id": int(r["id"]),
            "rel_path": r["rel_path"],
            "extension": r["extension"],
            "language": r["language"],
            "line_count": int(r["line_count"] or 0),
            "mtime": float(r["mtime"]),
            "last_indexed_at": r["last_indexed_at"],
            "module_docstring": r["module_docstring"],
            "external_imports": json.loads(r["external_imports_json"] or "[]"),
            "internal_imports": json.loads(r["internal_imports_json"] or "[]"),
        }
        for r in file_rows
    ]
    symbols = [
        {
            "name": r["symbol_name"],
            "type": r["symbol_type"],
            "signature": r["signature"],
            "docstring": r["docstring"],
            "start_line": int(r["start_line"] or 0),
            "end_line": int(r["end_line"] or 0),
            "is_public": bool(int(r["is_public"] or 0)),
            "rel_path": r["rel_path"],
        }
        for r in symbol_rows
    ]

    return {
        "repo": {
            "repo_id": repo_id,
            "repo_root": str(repo_root),
            "repo_name": repo["repo_name"],
            "first_indexed_at": repo["first_indexed_at"],
            "last_indexed_at": repo["last_indexed_at"],
            "total_file_count": int(repo["total_file_count"]),
            "total_line_count": int(repo["total_line_count"]),
            "total_symbol_count": symbol_count,
            "language_distribution": {str(r["language"] or "unknown"): int(r["c"]) for r in lang_rows},
            "directory_stats": json.loads(repo["directory_stats_json"] or "{}"),
        },
        "files": files,
        "symbols": symbols,
        "truncated": {
            "files": (not include_all) and (len(files) >= files_limit),
            "symbols": (not include_all) and (len(symbols) >= symbols_limit),
        },
    }


@app.get("/index/code/layer1/repo")
def index_code_layer1_repo(path: str = Query(".")):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")
    _init_code_index_db()
    conn = _code_db_conn()
    repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    repo = conn.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
    lang_rows = conn.execute(
        "SELECT language, COUNT(*) AS c FROM files WHERE repo_id=? GROUP BY language ORDER BY c DESC",
        (repo_id,),
    ).fetchall()
    symbol_count = int(conn.execute("SELECT COUNT(*) FROM symbols WHERE repo_id=?", (repo_id,)).fetchone()[0])
    conn.close()
    return {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "repo_name": repo["repo_name"],
        "first_indexed_at": repo["first_indexed_at"],
        "last_indexed_at": repo["last_indexed_at"],
        "total_file_count": int(repo["total_file_count"]),
        "total_line_count": int(repo["total_line_count"]),
        "total_symbol_count": symbol_count,
        "language_distribution": {str(r["language"] or "unknown"): int(r["c"]) for r in lang_rows},
        "directory_stats": json.loads(repo["directory_stats_json"] or "{}"),
        "db_path": str(CODE_INDEX_DB),
    }


@app.get("/index/code/layer1/files")
def index_code_layer1_files(
    path: str = Query("."),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")
    _init_code_index_db()
    conn = _code_db_conn()
    repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    rows = conn.execute(
        """
        SELECT id, rel_path, extension, language, line_count, mtime, last_indexed_at, module_docstring,
               external_imports_json, internal_imports_json
        FROM files
        WHERE repo_id=?
        ORDER BY rel_path
        LIMIT ? OFFSET ?
        """,
        (repo_id, limit, offset),
    ).fetchall()
    conn.close()
    files = []
    for r in rows:
        files.append(
            {
                "file_id": int(r["id"]),
                "rel_path": r["rel_path"],
                "extension": r["extension"],
                "language": r["language"],
                "line_count": int(r["line_count"] or 0),
                "mtime": float(r["mtime"]),
                "last_indexed_at": r["last_indexed_at"],
                "module_docstring": r["module_docstring"],
                "external_imports": json.loads(r["external_imports_json"] or "[]"),
                "internal_imports": json.loads(r["internal_imports_json"] or "[]"),
            }
        )
    return {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "count": len(files),
        "limit": limit,
        "offset": offset,
        "files": files,
    }


@app.get("/index/code/layer1/symbols")
def index_code_layer1_symbols(
    path: str = Query("."),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")
    _init_code_index_db()
    conn = _code_db_conn()
    repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    if q and q.strip():
        rows = conn.execute(
            """
            SELECT s.symbol_name, s.symbol_type, s.signature, s.docstring, s.start_line, s.end_line,
                   s.is_public, f.rel_path
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            WHERE s.repo_id=? AND LOWER(s.symbol_name) LIKE ?
            ORDER BY s.symbol_name, f.rel_path
            LIMIT ? OFFSET ?
            """,
            (repo_id, f"%{q.strip().lower()}%", limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.symbol_name, s.symbol_type, s.signature, s.docstring, s.start_line, s.end_line,
                   s.is_public, f.rel_path
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            WHERE s.repo_id=?
            ORDER BY s.symbol_name, f.rel_path
            LIMIT ? OFFSET ?
            """,
            (repo_id, limit, offset),
        ).fetchall()
    conn.close()
    symbols = []
    for r in rows:
        symbols.append(
            {
                "name": r["symbol_name"],
                "type": r["symbol_type"],
                "signature": r["signature"],
                "docstring": r["docstring"],
                "start_line": int(r["start_line"] or 0),
                "end_line": int(r["end_line"] or 0),
                "is_public": bool(int(r["is_public"] or 0)),
                "rel_path": r["rel_path"],
            }
        )
    return {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "count": len(symbols),
        "limit": limit,
        "offset": offset,
        "query": q,
        "symbols": symbols,
    }


@app.get("/index/code/context")
def index_code_context(
    path: str = Query("."),
    force_reindex: bool = Query(False),
    include_all: bool = Query(True),
    files_limit: int = Query(500, ge=1, le=20000),
    symbols_limit: int = Query(2000, ge=1, le=100000),
    threshold: int = Query(40, ge=0, le=100),
    max_scan_files: int = Query(5000, ge=100, le=20000),
):
    """
    Combined Layer 1 + Layer 2 payload for codebase-aware agents.
    Returns:
      - layer2_detection: project detection/classification signals
      - layer1_index: repository/file/symbol facts from SQLite
    """
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    layer2 = analyze_code_directory(root, threshold=threshold, max_scan_files=max_scan_files)
    if not layer2.get("ok"):
        raise HTTPException(500, layer2.get("error", "code analysis failed"))

    build_result = None
    if force_reindex:
        build_result = build_code_layer1_index(root, force=True, max_files=200000)
        if build_result.get("status") not in {"ok", "skipped_not_code_directory"}:
            raise HTTPException(500, str(build_result))
    else:
        # Ensure layer1 exists at least once.
        _init_code_index_db()
        conn = _code_db_conn()
        try:
            _resolve_repo_id_for_path(conn, root)
        except HTTPException:
            build_result = build_code_layer1_index(root, force=False, max_files=200000)
            if build_result.get("status") not in {"ok", "skipped_not_code_directory"}:
                conn.close()
                raise HTTPException(500, str(build_result))
        finally:
            conn.close()

    _init_code_index_db()
    conn = _code_db_conn()
    repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    layer1 = _fetch_layer1_payload(
        conn=conn,
        repo_id=repo_id,
        repo_root=repo_root,
        include_all=include_all,
        files_limit=files_limit,
        symbols_limit=symbols_limit,
    )
    conn.close()

    return {
        "ok": True,
        "input_path": str(root),
        "layer2_detection": layer2,
        "layer1_index": layer1,
        "build_result": build_result,
        "db_path": str(CODE_INDEX_DB),
    }


def _recent_changes_payload(repo_root: Path, recent_days: int = 7, limit: int = 20) -> dict[str, Any]:
    _init_code_index_db()
    conn = _code_db_conn()
    cutoff_ts = time.time() - (max(1, recent_days) * 86400)
    rows = conn.execute(
        """
        SELECT relative_path, last_modified, line_count
        FROM project_files
        WHERE repo_path=? AND last_modified>=?
        ORDER BY last_modified DESC
        LIMIT ?
        """,
        (str(repo_root), float(cutoff_ts), int(limit)),
    ).fetchall()
    conn.close()

    files = [
        {
            "relative_path": r["relative_path"],
            "last_modified": float(r["last_modified"]),
            "line_count": int(r["line_count"] or 0),
        }
        for r in rows
    ]

    commits: list[dict[str, Any]] = []
    if (repo_root / ".git").exists():
        try:
            out = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "log",
                    "--since",
                    f"{max(1, recent_days)} days ago",
                    "--pretty=format:%H|%ct|%s",
                    "-n",
                    str(int(limit)),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append(
                        {
                            "commit": parts[0],
                            "timestamp": int(parts[1]),
                            "message": parts[2],
                        }
                    )
        except Exception:
            pass
    return {"files": files, "commits": commits}


@app.get("/index/code/get_codebase_index")
def get_codebase_index_api(
    path: str = Query("."),
    recent_days: int = Query(7, ge=1, le=90),
    recent_limit: int = Query(20, ge=1, le=100),
    symbol_limit: int = Query(1200, ge=1, le=10000),
    force_reindex: bool = Query(False),
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    if force_reindex:
        built = build_code_layer1_index(root, force=True, max_files=200000)
        if built.get("status") not in {"ok", "skipped_not_code_directory"}:
            raise HTTPException(500, str(built))

    layer2 = analyze_code_directory(root)
    _init_code_index_db()
    conn = _code_db_conn()
    repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    layer1 = _fetch_layer1_payload(
        conn=conn,
        repo_id=repo_id,
        repo_root=repo_root,
        include_all=False,
        files_limit=300,
        symbols_limit=max(1, symbol_limit),
    )
    ext_rows = conn.execute(
        """
        SELECT package_name, version, dependency_type, source_file
        FROM external_dependencies
        WHERE repo_path=?
        ORDER BY package_name
        """,
        (str(repo_root),),
    ).fetchall()
    pf_rows = conn.execute(
        """
        SELECT relative_path, is_entry_point, is_test_file
        FROM project_files
        WHERE repo_path=?
        ORDER BY relative_path
        LIMIT 5000
        """,
        (str(repo_root),),
    ).fetchall()
    conn.close()

    symbols_index = [
        {
            "name": s["name"],
            "type": s["type"],
            "path": s["rel_path"],
            "line": s["start_line"],
        }
        for s in layer1["symbols"]
        if bool(s.get("is_public", True))
    ]
    external_deps = [
        {
            "package_name": r["package_name"],
            "version": r["version"],
            "dependency_type": r["dependency_type"],
            "source_file": r["source_file"],
        }
        for r in ext_rows
    ]
    recent = _recent_changes_payload(repo_root, recent_days=recent_days, limit=recent_limit)
    structure = {
        "file_count": layer1["repo"]["total_file_count"],
        "line_count": layer1["repo"]["total_line_count"],
        "languages": layer1["repo"]["language_distribution"],
        "directories": layer1["repo"]["directory_stats"],
        "entry_points": [r["relative_path"] for r in pf_rows if int(r["is_entry_point"] or 0) == 1][:100],
        "test_file_count": sum(1 for r in pf_rows if int(r["is_test_file"] or 0) == 1),
    }

    return {
        "ok": True,
        "repo_path": str(repo_root),
        "layer2_project_detection": layer2,
        "structure": structure,
        "symbols_index": symbols_index,
        "external_dependencies": external_deps,
        "recent_changes": recent,
        "db_path": str(CODE_INDEX_DB),
    }


@app.post("/index/code/get_module_detail")
def get_module_detail_api(
    repo_path: str = Body(..., embed=True),
    paths: list[str] = Body(..., embed=True),
):
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")
    _init_code_index_db()
    conn = _code_db_conn()
    _repo_id, repo_root = _resolve_repo_id_for_path(conn, root)

    modules = []
    for rel in paths:
        rel_clean = str(rel).replace("\\", "/").lstrip("./")
        file_row = conn.execute(
            """
            SELECT file_path, relative_path, extension, line_count, is_entry_point, is_test_file, is_config_file
            FROM project_files
            WHERE repo_path=? AND relative_path=?
            """,
            (str(repo_root), rel_clean),
        ).fetchone()
        if not file_row:
            continue
        sym_rows = conn.execute(
            """
            SELECT symbol_name, symbol_type, signature, docstring_brief, line_start, line_end, is_public, language
            FROM code_symbols
            WHERE repo_path=? AND relative_path=?
            ORDER BY line_start
            """,
            (str(repo_root), rel_clean),
        ).fetchall()
        imports_row = conn.execute(
            "SELECT external_imports_json, internal_imports_json, module_docstring FROM files WHERE abs_path=?",
            (file_row["file_path"],),
        ).fetchone()
        modules.append(
            {
                "relative_path": file_row["relative_path"],
                "extension": file_row["extension"],
                "line_count": int(file_row["line_count"] or 0),
                "is_entry_point": bool(int(file_row["is_entry_point"] or 0)),
                "is_test_file": bool(int(file_row["is_test_file"] or 0)),
                "is_config_file": bool(int(file_row["is_config_file"] or 0)),
                "module_docstring": imports_row["module_docstring"] if imports_row else None,
                "imports": {
                    "external": json.loads(imports_row["external_imports_json"] or "[]") if imports_row else [],
                    "internal": json.loads(imports_row["internal_imports_json"] or "[]") if imports_row else [],
                },
                "symbols": [
                    {
                        "name": s["symbol_name"],
                        "type": s["symbol_type"],
                        "signature": s["signature"],
                        "docstring_brief": s["docstring_brief"],
                        "line_start": int(s["line_start"] or 0),
                        "line_end": int(s["line_end"] or 0),
                        "is_public": bool(int(s["is_public"] or 0)),
                        "language": s["language"],
                    }
                    for s in sym_rows
                ],
            }
        )
    conn.close()
    return {"ok": True, "repo_path": str(repo_root), "count": len(modules), "modules": modules}


@app.get("/index/code/get_file_content")
def get_file_content_api(
    repo_path: str = Query(...),
    path: str = Query(...),
    start_line: int = Query(1, ge=1),
    end_line: int | None = Query(None, ge=1),
):
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    rel = str(path).replace("\\", "/").lstrip("./")
    abs_path = (root / rel).resolve()
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(404, "File not found")
    if not _is_path_allowed(abs_path):
        raise HTTPException(403, "Path not allowed")

    text = _safe_read_text(abs_path)
    lines = text.splitlines()
    start = max(1, int(start_line))
    end = int(end_line) if end_line is not None else len(lines)
    end = max(start, min(end, len(lines)))
    snippet = "\n".join(lines[start - 1 : end])
    return {
        "ok": True,
        "repo_path": str(root),
        "relative_path": rel,
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "content": snippet,
    }


# ── /index/code/search_chunks ────────────────────────────────
def _code_query_tokens(query: str) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in re.findall(r"\b\w+\b", q):
        if len(tok) < 2:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _split_code_chunks_by_lines(text: str, chunk_lines: int, chunk_overlap: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []

    step = max(1, int(chunk_lines) - int(chunk_overlap))
    out: list[dict[str, Any]] = []
    i = 0
    idx = 0
    total_lines = len(lines)
    while i < total_lines:
        end = min(total_lines, i + int(chunk_lines))
        chunk_text = "\n".join(lines[i:end]).strip()
        if chunk_text:
            out.append(
                {
                    "chunk_index": idx,
                    "start_line": i + 1,
                    "end_line": end,
                    "chunk": chunk_text,
                }
            )
            idx += 1
        if end >= total_lines:
            break
        i += step

    total_chunks = len(out)
    for c in out:
        c["chunk_total"] = total_chunks
    return out


def _encode_code_chunk_id(rel_path: str, chunk_index: int, chunk_lines: int, chunk_overlap: int) -> str:
    payload = {
        "p": rel_path.replace("\\", "/"),
        "idx": int(chunk_index),
        "cl": int(chunk_lines),
        "ov": int(chunk_overlap),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _score_code_chunk(query: str, tokens: list[str], rel_path: str, chunk_text: str, base_score: float) -> tuple[float, list[str]]:
    q = (query or "").strip().lower()
    rel_l = rel_path.lower()
    txt_l = chunk_text.lower()
    matched_tokens: list[str] = []
    score = float(base_score)

    if q and q in rel_l:
        score += 2.5
    if q and q in txt_l:
        score += 4.0

    for tok in tokens:
        path_hit = tok in rel_l
        txt_count = txt_l.count(tok)
        if path_hit or txt_count > 0:
            matched_tokens.append(tok)
        if path_hit:
            score += 1.5
        if txt_count > 0:
            score += min(4, txt_count) * 0.9

    return score, matched_tokens


def _candidate_code_files_for_query(
    conn: sqlite3.Connection,
    repo_root: Path,
    tokens: list[str],
    candidate_files: int,
) -> list[dict[str, Any]]:
    repo_s = str(repo_root)
    scored: dict[str, dict[str, Any]] = {}
    tok_slice = tokens[:8]

    def _upsert(file_path: str, rel_path: str, line_count: int, delta: float, reason: str) -> None:
        key = str(file_path)
        existing = scored.get(key)
        if existing is None:
            scored[key] = {
                "file_path": str(file_path),
                "relative_path": str(rel_path).replace("\\", "/"),
                "line_count": int(line_count or 0),
                "score": float(delta),
                "reasons": [reason],
            }
            return
        existing["score"] = float(existing.get("score", 0.0)) + float(delta)
        reasons = existing.get("reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        existing["reasons"] = reasons

    if tok_slice:
        for tok in tok_slice:
            like = f"%{tok}%"
            path_rows = conn.execute(
                """
                SELECT file_path, relative_path, line_count
                FROM project_files
                WHERE repo_path = ? AND LOWER(relative_path) LIKE ?
                ORDER BY is_entry_point DESC, last_modified DESC
                LIMIT 300
                """,
                (repo_s, like),
            ).fetchall()
            for r in path_rows:
                _upsert(
                    file_path=str(r["file_path"]),
                    rel_path=str(r["relative_path"]),
                    line_count=int(r["line_count"] or 0),
                    delta=2.0,
                    reason=f"path:{tok}",
                )

            symbol_rows = conn.execute(
                """
                SELECT DISTINCT cs.file_path, cs.relative_path, COALESCE(pf.line_count, 0) AS line_count
                FROM code_symbols cs
                LEFT JOIN project_files pf ON pf.file_path = cs.file_path
                WHERE cs.repo_path = ? AND LOWER(cs.symbol_name) LIKE ?
                LIMIT 500
                """,
                (repo_s, like),
            ).fetchall()
            for r in symbol_rows:
                _upsert(
                    file_path=str(r["file_path"]),
                    rel_path=str(r["relative_path"]),
                    line_count=int(r["line_count"] or 0),
                    delta=3.0,
                    reason=f"symbol:{tok}",
                )

    fallback_rows = conn.execute(
        """
        SELECT file_path, relative_path, line_count
        FROM project_files
        WHERE repo_path = ?
        ORDER BY is_entry_point DESC, is_test_file ASC, last_modified DESC
        LIMIT ?
        """,
        (repo_s, max(200, int(candidate_files) * 2)),
    ).fetchall()
    for r in fallback_rows:
        _upsert(
            file_path=str(r["file_path"]),
            rel_path=str(r["relative_path"]),
            line_count=int(r["line_count"] or 0),
            delta=0.25,
            reason="fallback",
        )

    ranked = sorted(
        scored.values(),
        key=lambda x: (float(x.get("score", 0.0)), -int(x.get("line_count", 0))),
        reverse=True,
    )
    return ranked[: max(10, int(candidate_files))]


@app.get("/index/code/search_chunks")
def search_code_chunks_api(
    repo_path: str = Query("."),
    query: str = Query(..., min_length=1),
    top_k: int = Query(8, ge=1, le=50),
    candidate_files: int = Query(200, ge=10, le=2000),
    chunk_lines: int = Query(80, ge=20, le=400),
    chunk_overlap: int = Query(20, ge=0, le=200),
    max_chars: int = Query(1600, ge=200, le=4000),
    use_semantic: bool = Query(True),
    semantic_candidates: int = Query(240, ge=20, le=2000),
    lexical_weight: float = Query(1.0, ge=0.0, le=10.0),
    semantic_weight: float = Query(6.0, ge=0.0, le=20.0),
):
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    q = query.strip()
    if not q:
        raise HTTPException(400, "empty query")

    bounded_top_k = max(1, min(int(top_k), 50))
    bounded_candidate_files = max(10, min(int(candidate_files), 2000))
    bounded_chunk_lines = max(20, min(int(chunk_lines), 400))
    bounded_chunk_overlap = max(0, min(int(chunk_overlap), 200))
    bounded_max_chars = max(200, min(int(max_chars), 4000))
    bounded_semantic_candidates = max(20, min(int(semantic_candidates), 2000))

    _init_code_index_db()
    conn = _code_db_conn()
    _repo_id, repo_root = _resolve_repo_id_for_path(conn, root)
    tokens = _code_query_tokens(q)
    candidates = _candidate_code_files_for_query(conn, repo_root, tokens, bounded_candidate_files)
    conn.close()

    merged_rows: dict[tuple[str, int], dict[str, Any]] = {}
    scanned_files = 0
    lexical_hits = 0
    semantic_hits = 0

    def _merge_result(
        base: dict[str, Any],
        *,
        lexical_score: float,
        semantic_score: float,
        matched_tokens: list[str],
        source: str,
    ) -> None:
        key = (str(base["relative_path"]), int(base["chunk_index"]))
        final = (float(lexical_weight) * float(lexical_score)) + (
            float(semantic_weight) * float(semantic_score)
        )
        if final <= 0.0:
            return
        existing = merged_rows.get(key)
        if existing is None:
            row = dict(base)
            row["lexical_score"] = float(lexical_score)
            row["semantic_score"] = float(semantic_score)
            row["matched_tokens"] = sorted(set(matched_tokens))
            row["retrieval_sources"] = [source]
            row["score"] = float(final)
            merged_rows[key] = row
            return

        existing["lexical_score"] = max(float(existing.get("lexical_score", 0.0)), float(lexical_score))
        existing["semantic_score"] = max(float(existing.get("semantic_score", 0.0)), float(semantic_score))
        existing["score"] = (float(lexical_weight) * float(existing["lexical_score"])) + (
            float(semantic_weight) * float(existing["semantic_score"])
        )
        token_set = set(existing.get("matched_tokens", []))
        token_set.update(matched_tokens)
        existing["matched_tokens"] = sorted(token_set)
        srcs = list(existing.get("retrieval_sources", []))
        if source not in srcs:
            srcs.append(source)
        existing["retrieval_sources"] = srcs

    for c in candidates:
        if scanned_files >= bounded_candidate_files:
            break
        abs_path = Path(str(c["file_path"])).resolve()
        if not abs_path.exists() or not abs_path.is_file():
            continue
        if not _is_path_allowed(abs_path):
            continue

        try:
            content = _safe_read_text(abs_path)
        except Exception:
            continue
        if not content.strip():
            continue

        rel_path = str(c["relative_path"]).replace("\\", "/")
        chunks = _split_code_chunks_by_lines(
            content,
            chunk_lines=bounded_chunk_lines,
            chunk_overlap=bounded_chunk_overlap,
        )
        if not chunks:
            continue
        scanned_files += 1

        base_score = float(c.get("score", 0.0))
        for ch in chunks:
            score, matched = _score_code_chunk(
                query=q,
                tokens=tokens,
                rel_path=rel_path,
                chunk_text=str(ch["chunk"]),
                base_score=base_score,
            )
            if score <= 0.0:
                continue
            if not matched and q.lower() not in str(ch["chunk"]).lower() and q.lower() not in rel_path.lower():
                continue
            text = str(ch["chunk"])
            if len(text) > bounded_max_chars:
                text = text[:bounded_max_chars].rstrip() + "\n..."
            _merge_result(
                {
                    "relative_path": rel_path,
                    "file_path": str(abs_path),
                    "chunk": text,
                    "chunk_index": int(ch["chunk_index"]),
                    "chunk_total": int(ch["chunk_total"]),
                    "start_line": int(ch["start_line"]),
                    "end_line": int(ch["end_line"]),
                    "chunk_id": _encode_code_chunk_id(
                        rel_path=rel_path,
                        chunk_index=int(ch["chunk_index"]),
                        chunk_lines=bounded_chunk_lines,
                        chunk_overlap=bounded_chunk_overlap,
                    ),
                },
                lexical_score=float(score),
                semantic_score=0.0,
                matched_tokens=matched,
                source="lexical",
            )
            lexical_hits += 1

    semantic_state: dict[str, Any] = {"ok": False, "ready": False, "used": False}
    if bool(use_semantic):
        semantic_state = _ensure_code_chunk_annoy_ready()
        if bool(semantic_state.get("ready")):
            semantic_state["used"] = True
            query_vec = _embed_code_text(q)
            ann_hits = _search_code_chunk_annoy(
                query_vector=query_vec,
                top_k=max(bounded_semantic_candidates, bounded_top_k * 20),
            )
            if ann_hits:
                hit_by_id = {int(h["chunk_id"]): float(h.get("semantic_score", 0.0)) for h in ann_hits}
                ids = list(hit_by_id.keys())
                rows: list[sqlite3.Row] = []
                conn = _code_db_conn()
                for batch_start in range(0, len(ids), 900):
                    part = ids[batch_start : batch_start + 900]
                    if not part:
                        continue
                    placeholders = ",".join("?" for _ in part)
                    rows.extend(
                        conn.execute(
                            f"""
                            SELECT
                                cc.id,
                                cc.file_path,
                                cc.relative_path,
                                cc.chunk_index,
                                cc.start_line,
                                cc.end_line,
                                cc.chunk_text,
                                (
                                    SELECT COUNT(*)
                                    FROM code_chunks c2
                                    WHERE c2.repo_path = cc.repo_path
                                      AND c2.file_path = cc.file_path
                                ) AS chunk_total
                            FROM code_chunks cc
                            WHERE cc.repo_path = ?
                              AND cc.id IN ({placeholders})
                            """,
                            (str(repo_root), *part),
                        ).fetchall()
                    )
                conn.close()

                for r in rows:
                    sem_score = float(hit_by_id.get(int(r["id"]), 0.0))
                    if sem_score <= 0.0:
                        continue
                    rel_path = str(r["relative_path"]).replace("\\", "/")
                    raw_chunk = str(r["chunk_text"] or "")
                    lex_hint, matched = _score_code_chunk(
                        query=q,
                        tokens=tokens,
                        rel_path=rel_path,
                        chunk_text=raw_chunk,
                        base_score=0.0,
                    )
                    if not matched and q.lower() not in raw_chunk.lower() and q.lower() not in rel_path.lower():
                        continue
                    text = raw_chunk
                    if len(text) > bounded_max_chars:
                        text = text[:bounded_max_chars].rstrip() + "\n..."
                    _merge_result(
                        {
                            "relative_path": rel_path,
                            "file_path": str(r["file_path"]),
                            "chunk": text,
                            "chunk_index": int(r["chunk_index"]),
                            "chunk_total": int(r["chunk_total"] or 0),
                            "start_line": int(r["start_line"] or 1),
                            "end_line": int(r["end_line"] or 1),
                            "chunk_id": _encode_code_chunk_id(
                                rel_path=rel_path,
                                chunk_index=int(r["chunk_index"]),
                                chunk_lines=bounded_chunk_lines,
                                chunk_overlap=bounded_chunk_overlap,
                            ),
                        },
                        lexical_score=max(0.0, float(lex_hint)),
                        semantic_score=sem_score,
                        matched_tokens=matched,
                        source="semantic",
                    )
                    semantic_hits += 1

    deduped = sorted(
        merged_rows.values(),
        key=lambda r: float(r.get("score", 0.0)),
        reverse=True,
    )[:bounded_top_k]

    return {
        "ok": True,
        "repo_path": str(repo_root),
        "query": q,
        "top_k": int(bounded_top_k),
        "candidate_files": int(bounded_candidate_files),
        "chunk_lines": int(bounded_chunk_lines),
        "chunk_overlap": int(bounded_chunk_overlap),
        "scanned_files": int(scanned_files),
        "lexical_hits": int(lexical_hits),
        "semantic_hits": int(semantic_hits),
        "semantic": {
            "enabled": bool(use_semantic),
            "used": bool(semantic_state.get("used", False)),
            "ready": bool(semantic_state.get("ready", False)),
            "status": semantic_state,
            "weights": {
                "lexical": float(lexical_weight),
                "semantic": float(semantic_weight),
            },
        },
        "result_count": len(deduped),
        "results": deduped,
    }

@app.get("/image/index/status")
def image_index_status():
    indexed_images = 0
    indexed_images_with_ocr = 0
    capabilities = _image_v2_capabilities()
    status = "ok"
    annoy_status: dict[str, Any] = {
        "installed": False,
        "index_exists": False,
        "needs_rebuild": False,
        "vector_ready_images": 0,
        "ready": False,
    }

    try:
        from image_search_implementation_v2.annoy_store import get_annoy_status
        from image_search_implementation_v2.db import count_images, count_ocr_images, init_db

        init_db()
        indexed_images = int(count_images())
        indexed_images_with_ocr = int(count_ocr_images())
        annoy_status = get_annoy_status()
    except Exception as e:
        status = "degraded"
        capabilities["error"] = str(e)

    ocr_coverage = float(indexed_images_with_ocr / indexed_images) if indexed_images else 0.0
    return {
        "status": status,
        "engine": "annoy_sqlite_ocr",
        "indexed_images": indexed_images,
        "indexed_images_with_ocr": indexed_images_with_ocr,
        "ocr_coverage": round(ocr_coverage, 4),
        "capabilities": capabilities,
        "annoy": annoy_status,
        "annoy_exists": bool(annoy_status.get("index_exists")),
        "annoy_needs_rebuild": bool(annoy_status.get("needs_rebuild")),
    }


# ── Admin ─────────────────────────────────────────────────────
@app.post("/admin/prewarm/llm")
async def prewarm_llm():
    """Load llama-server now so the first real /llm call is instant."""
    async with rm.llm_context():
        return {
            "status": "ok",
            "note":   "llama-server warm and resident",
            "pid":    rm._llm_proc.pid if rm._llm_proc else None,
        }


@app.post("/admin/prewarm/clip")
async def prewarm_clip():
    """Pre-load CLIP so the first /search call doesn't pay load time."""
    async with rm.embed_context():
        try:
            lazy_load_clip()
            return {"status": "ok", "note": "CLIP loaded"}
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.post("/admin/force-switch/llm")
async def force_switch_llm():
    """Force immediate switch to LLM mode (stops embed models)."""
    if rm._switch_lock.locked():
        return {"status": "busy", "note": "switch already in progress"}
    async with rm.llm_context():
        return {"status": "ok", "resource_state": rm._state,
                "pid": rm._llm_proc.pid if rm._llm_proc else None}


@app.post("/admin/force-switch/embed")
async def force_switch_embed():
    """Force immediate switch to Embed mode (stops llama-server)."""
    if rm._switch_lock.locked():
        return {"status": "busy", "note": "switch already in progress"}
    async with rm.embed_context():
        return {"status": "ok", "resource_state": rm._state}


# ── File serving ──────────────────────────────────────────────
@app.get("/files")
def get_file(path: str = Query(...)):
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    if not _is_path_allowed(p):
        raise HTTPException(403, "Path not allowed")
    return FileResponse(
        path=p, filename=p.name, media_type=None,
        headers={"Content-Disposition": f'inline; filename="{p.name}"'},
    )


@app.get("/files/list")
def list_files(
    directory: str = Query(...),
    recursive: bool = Query(True),
    limit: int = Query(200, ge=1, le=2000),
    pattern: str = Query("*"),
):
    root = Path(directory).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "Directory not found")
    if not _is_path_allowed(root):
        raise HTTPException(403, "Path not allowed")

    walker = root.rglob("*") if recursive else root.glob("*")
    out = []
    for p in walker:
        if not p.is_file():
            continue
        if pattern and not fnmatch.fnmatch(p.name, pattern):
            continue
        if should_ignore(p):
            continue
        mime, _ = mimetypes.guess_type(str(p))
        out.append(
            {
                "path": str(p),
                "filename": p.name,
                "size_bytes": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "mime_type": mime or "application/octet-stream",
            }
        )
        if len(out) >= limit:
            break
    return {"directory": str(root), "count": len(out), "files": out}


@app.post("/files/preflight")
def preflight_file_add(
    relative_dir: str = Body(..., embed=True),
    filename:     str = Body(..., embed=True),
    sha256:       str = Body(..., embed=True),
):
    target_dir = (ORGANIZED_ROOT / relative_dir).resolve()
    if ORGANIZED_ROOT not in target_dir.parents and target_dir != ORGANIZED_ROOT:
        raise HTTPException(403, "Invalid directory")
    target = target_dir / filename
    if not target.exists():
        return {"action": "accept", "filename": filename}
    if sha256_file(target) == sha256:
        return {"action": "reject", "reason": "Exact duplicate already exists"}
    i = 1
    while True:
        c = target_dir / f"{target.stem}[{i}]{target.suffix}"
        if not c.exists():
            return {"action": "rename", "filename": c.name}
        i += 1


@app.get("/activity/recent")
def recent_activity():
    return {"items": get_recent_syncs()}


@app.post("/thumbnails/fetch")
def fetch_thumbnails(
    category: str       = Body(...),
    paths:    list[str] = Body(...),
):
    results = []
    for p in paths:
        src = Path(p)
        if not src.exists() or should_ignore(src):
            continue
        data = read_thumbnail(src, category)
        if data:
            results.append({
                "path":      p,
                "thumbnail": base64.b64encode(data).decode(),
                "mime":      "image/jpeg",
            })
    return {"count": len(results), "thumbnails": results}


@app.get("/storage/usage")
def storage_usage():
    from config import get_storage_dir
    storage_path = str(get_storage_dir())
    if not os.path.exists(storage_path):
        raise HTTPException(404, "Storage not found")
    s     = os.statvfs(storage_path)
    total = s.f_frsize * s.f_blocks
    free  = s.f_frsize * s.f_bavail
    used  = total - free
    return {
        "path":         storage_path,
        "total_bytes":  total,
        "used_bytes":   used,
        "free_bytes":   free,
        "used_percent": round(used / total * 100, 2) if total else 0.0,
    }

# middleware protection definition

@app.middleware("http")
async def network_guard(request: Request, call_next):
    path = request.url.path

    # Always allow health + network endpoints
    allowed_paths = [
        "/health",
        "/network/status",
        "/wifi/scan",
        "/wifi/connect","/configure-wifi"
    ]

    if path in allowed_paths:
        return await call_next(request)

    # If not connected and not hotspot active → block
    if not NETWORK_STATE["connected"] and not NETWORK_STATE["hotspot_active"]:
        return JSONResponse(
            status_code=503,
            content={"error": "Network not ready"}
        )

    return await call_next(request)

# network status endpoint
@app.get("/network/status")
def network_status():
    return {
        "connected": NETWORK_STATE["connected"],
        "ssid": NETWORK_STATE["ssid"],
        "hotspot_active": NETWORK_STATE["hotspot_active"],
        "hotspot_name": HOTSPOT_NAME if NETWORK_STATE["hotspot_active"] else None
    }

@app.post("/configure-wifi")
def configure_wifi(data: dict):
    if not NETWORK_CONTROL_SUPPORTED:
        raise HTTPException(501, "WiFi configuration via nmcli is not supported on this host")

    ssid = data.get("ssid")
    password = data.get("password")

    if not ssid or not password:
        raise HTTPException(400, "Missing SSID or password")

    try:
        # 1️⃣ Stop hotspot FIRST
        subprocess.run(["nmcli", "connection", "down", HOTSPOT_NAME], check=False)
        subprocess.run(["nmcli", "connection", "delete", HOTSPOT_NAME], check=False)

        time.sleep(2)  # give interface time to reset

        # 2️⃣ Rescan WiFi
        subprocess.run(["nmcli", "device", "wifi", "rescan"], check=False)

        # 3️⃣ Connect
        subprocess.run(
            ["nmcli", "device", "wifi", "connect", ssid, "password", password],
            check=True
        )

        return {"status": "connected"}

    except subprocess.CalledProcessError:
        raise HTTPException(500, "Failed to connect to WiFi")

@app.post("/storage/init")
def init_storage(storage_type: str):
    session_id = start_auth(storage_type)
    return {"session_id": session_id}


@app.get("/storage/poll")
def poll_storage(session_id: str):

    session = auth_sessions.get(session_id)

    if not session:
        return {"error": "invalid_session"}

    return {
        "status": session["status"],
        "verification_url": session["verification_url"],
        "user_code": session["user_code"]
    }

@app.get("/storage/status")
def storage_status(remote_name: str):
    return {"connected": check_remote(remote_name)}


@app.post("/sync/start")
def sync_start(remote_name: str):
    local_path = f"/opt/yourapp/data/{remote_name}"
    return start_copy(remote_name, local_path)


@app.get("/sync/status")
def sync_status(job_id: int):
    return get_job_status(job_id)

@app.post("/storage/finalize")
def finalize_storage(session_id: str, remote_name: str):

    session = auth_sessions.get(session_id)

    if not session or session["status"] != "completed":
        return {"error": "not_ready"}

    token_json = session["token"]

    # Create remote
    requests.post(
        f"{RCLONE_URL}/config/create",
        json={
            "name": remote_name,
            "type": "drive"
        }
    )

    # Inject token
    requests.post(
        f"{RCLONE_URL}/config/update",
        json={
            "name": remote_name,
            "parameters": {
                "token": token_json
            }
        }
    )

    return {"success": True}

# ── Utility ───────────────────────────────────────────────────
def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_path_allowed(p: Path) -> bool:
    if ALLOW_ARBITRARY_FILE_ACCESS:
        return True
    rp = p.resolve()
    for base in FILE_ROOTS:
        if rp == base or base in rp.parents:
            return True
    return False


def get_cpu_temp_c() -> float:
    """Return highest temperature across all thermal zones in °C."""
    highest = 0.0
    for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            val = float(zone.read_text().strip()) / 1000.0
            if val > highest:
                highest = val
        except Exception:
            continue
    return highest

def get_current_wifi():
    try:
        result = subprocess.check_output(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            text=True
        )
        for line in result.strip().split("\n"):
            if line.startswith("yes:"):
                ssid = line.split(":")[1]
                return ssid
        return None
    except Exception:
        return None

def stop_hotspot():
    subprocess.run(["nmcli", "connection", "down", HOTSPOT_NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    NETWORK_STATE["hotspot_active"] = False

def initialize_network():
    print("🔍 Checking network state on startup...")

    ssid = get_current_wifi()

    if ssid:
        print(f"✅ Connected to WiFi: {ssid}")
        NETWORK_STATE["connected"] = True
        NETWORK_STATE["ssid"] = ssid
        NETWORK_STATE["hotspot_active"] = False
    else:
        print("❌ Not connected to WiFi.")
        NETWORK_STATE["connected"] = False
        NETWORK_STATE["ssid"] = None
        start_hotspot()

def is_connected():
    try:
        result = subprocess.check_output(
            ["nmcli", "-t", "-f", "GENERAL.STATE", "device","show", WIFI_INTERFACE],
            text=True
        ).strip()
        if result.startswith("GENERAL.STATE:100"):
            return True
        return False

    except Exception as e:
        print("Network check failed:", e)
        return False

def stop_system_dnsmasq():
    if shutil.which("systemctl") is None:
        return
    subprocess.run(["sudo", "systemctl", "stop", "dnsmasq"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_hotspot():
    print("⚡ Starting Radxa Hotspot...")

    subprocess.run(["nmcli", "connection", "delete", HOTSPOT_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run([
        "nmcli", "connection", "add",
        "type", "wifi",
        "ifname", WIFI_INTERFACE,
        "mode", "ap",
        "con-name", HOTSPOT_NAME,
        "ssid", HOTSPOT_SSID
    ])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "802-11-wireless.band", "bg"])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "802-11-wireless-security.key-mgmt", "wpa-psk"])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "802-11-wireless-security.psk", HOTSPOT_PASSWORD])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "ipv4.method", "shared"])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "ipv4.addresses", HOTSPOT_IP])

    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_NAME,
                    "ipv6.method", "ignore"])

    subprocess.run(["nmcli", "connection", "up", HOTSPOT_NAME])

    time.sleep(3)

    print("✅ Hotspot active at 10.99.0.1")

def network_bootstrap():
    print("checking network state on startup...")
    if not NETWORK_CONTROL_SUPPORTED:
        print("nmcli not available; skipping hotspot bootstrap")
        NETWORK_STATE["connected"] = True
        NETWORK_STATE["ssid"] = "local-dev"
        NETWORK_STATE["hotspot_active"] = False
        return

    if is_connected():
        ssid = get_current_wifi()
        print(f"connected to wifi: {ssid}")
        NETWORK_STATE["connected"] = True
        NETWORK_STATE["ssid"] = ssid
        NETWORK_STATE["hotspot_active"] = False

    else:
        print("❌ Not connected. Enabling hotspot...")

        NETWORK_STATE["connected"] = False
        NETWORK_STATE["ssid"] = None

        stop_system_dnsmasq()
        start_hotspot()

        NETWORK_STATE["hotspot_active"] = True
