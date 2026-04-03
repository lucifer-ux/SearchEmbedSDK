# image_search_implementation_v2/config.py
from pathlib import Path
import sys
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config import get_organized_root, get_image_directory, get_storage_dir

BASE_DIR = get_organized_root()
try:
    IMAGE_FOLDER = get_image_directory().relative_to(BASE_DIR)
except ValueError:
    IMAGE_FOLDER = get_image_directory()

DATA_DIR = get_storage_dir() / "image_search_implementation_v2" / "storage"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "images_meta.db"
EMBEDDINGS_DIR = DATA_DIR / "embeddings"
EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
ANNOY_INDEX_PATH = DATA_DIR / "annoy_index.ann"
ANNOY_STATE_PATH = DATA_DIR / "annoy_state.json"

# Embedding model name (CLIP). This model will be loaded only in workers.
CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"  # reliable baseline; can swap later
VECTOR_DIM = 512
ANNOY_N_TREES = 12

# indexing / search tunables
ANN_TOPK = 50
FTS_TOPK = 50
UNION_LIMIT = 100

# fuzzy parameters
FUZZY_FILENAME_THRESHOLD = 70
