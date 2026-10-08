"""Source and PyInstaller entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from farming_accounts.app import main

if __name__ == "__main__":
    main()
