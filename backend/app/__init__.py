import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"DocTheCode needs Python 3.11 or newer, but this is Python {sys.version.split()[0]} "
        f"({sys.executable}). Recreate backend/.venv with a newer Python; see the README."
    )
