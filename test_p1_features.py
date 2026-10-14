"""兼容 shim（E4 拆分，v0.92）：用例已按域拆到 `test_*.py`，本文件只转发全量运行。

新用法：`python run_tests.py [域]`（见 run_tests.py）。
保留本文件是为了让旧命令、文档引用、CI 里的 `python test_p1_features.py` 继续能用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_tests  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_tests.main(sys.argv[1:]))
