#!/usr/bin/env python3
"""项目入口。用法见 `python run.py --help`。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pricerelat.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
