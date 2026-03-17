"""
backend/main.py — Root orchestrator (production entry point).
Calls each phase's main.py in sequence.

Usage (from project root):
    python backend/main.py <filename>
"""
import os
import subprocess
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_phase(phase_main: str, *args):
    result = subprocess.run([sys.executable, phase_main] + list(args))
    if result.returncode != 0:
        print(f"[PIPELINE] Phase failed: {phase_main}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python backend/main.py <filename>")
        sys.exit(1)

    filename  = sys.argv[1]
    file_stem = os.path.splitext(filename)[0]  # strip extension for Phase 2+

    # Phase 1: Initial processing (intake → text → classify → TOC → Supabase)
    _run_phase(os.path.join(BACKEND_DIR, "01_INITIAL", "main.py"), filename)

    # Phase 2: AST construction + semantic labeling
    _run_phase(os.path.join(BACKEND_DIR, "02_MIDDLE", "main.py"), "--file_name", file_stem)
