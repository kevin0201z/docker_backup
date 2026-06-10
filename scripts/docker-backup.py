#!/usr/bin/env python3
"""docker_backup 包的轻量可执行入口。"""

from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path，使得可以导入 docker_backup 包。
# 使用 resolve() 确保符号链接和相对路径都能正确解析。
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from docker_backup.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
