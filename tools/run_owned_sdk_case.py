"""Explicit Source19 candidate child; no execution occurs on import."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from pyaedt_motor.sdk_case_worker import main
if __name__ == '__main__': raise SystemExit(main(source_root=ROOT))
