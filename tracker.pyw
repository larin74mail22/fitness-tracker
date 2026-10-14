"""FITNESS Tracker без окна консоли: pythonw tracker.pyw (двойной щелчок, автозапуск). Значок появится в трее."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness.launcher.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
