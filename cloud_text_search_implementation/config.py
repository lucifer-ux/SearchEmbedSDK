from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ROOT_CONFIG_PATH = _ROOT / "config.py"
_SPEC = spec_from_file_location("root_config", _ROOT_CONFIG_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load root config module at {_ROOT_CONFIG_PATH}")
_ROOT_CONFIG = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ROOT_CONFIG)

BASE_DIR = _ROOT_CONFIG.get_organized_root()
TEXT_FOLDERS = ["docs", "spreadsheets"]
