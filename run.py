"""Source and PyInstaller entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if __name__ == "__main__":
    if '--signer-self-check' in sys.argv:
        from farming_accounts.self_check import signer_self_check
        signer_self_check()
    elif '--worker' in sys.argv:
        from farming_accounts.worker import main
        main([argument for argument in sys.argv[1:] if argument != '--worker'])
    else:
        from farming_accounts.app import main
        main()
