"""Explicit isolated diagnostic child; no inherited editable/PYTHONPATH package."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from pyaedt_motor.sdk_launch import run_child
if __name__=='__main__':raise SystemExit(run_child(sys.argv[1:],ROOT))
