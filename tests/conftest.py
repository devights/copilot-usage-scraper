import sys
from pathlib import Path

# Ensure repository root is importable in CI and local runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
