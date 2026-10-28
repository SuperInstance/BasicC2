import sys
from pathlib import Path

# Make the repo root importable (fleet is a top-level package there)
# regardless of the directory pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
