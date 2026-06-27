"""Single source of truth for on-disk locations. quant/ lives one level under
the repo root, so REPO_ROOT keeps .cache and state files exactly where they were
before the package move."""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, ".cache")
