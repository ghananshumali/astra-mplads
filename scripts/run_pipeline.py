"""Run the multi-agent pipeline over ingested data and persist flags."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astra.pipeline import run_pipeline

if __name__ == "__main__":
    run_pipeline()
