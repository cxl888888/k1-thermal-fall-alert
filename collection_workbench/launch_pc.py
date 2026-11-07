"""Open the local workbench with user-supplied --data-root and --voice-record paths."""
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    app = Path(__file__).with_name('app.py').resolve()
    raise SystemExit(subprocess.call([sys.executable, '-u', str(app), '--open', *sys.argv[1:]]))
