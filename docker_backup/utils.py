"""通用工具函数：大小展示、数据库提示和备份目录安全校验。"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import Container


DB_IMAGE_KEYWORDS = {
    "postgres": "PostgreSQL 容器建议配合 pg_dump 做逻辑备份。",
    "mysql": "MySQL 容器建议配合 mysqldump 做逻辑备份。",
    "mariadb": "MariaDB 容器建议配合 mysqldump 做逻辑备份。",
    "redis": "Redis 容器建议先执行 SAVE/BGSAVE 或确认持久化策略。",
    "mongo": "MongoDB 容器建议配合 mongodump 做逻辑备份。",
    "oracle": "Oracle 容器建议使用数据库自身导出工具配合文件备份。",
}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                total += child.lstat().st_size
        except OSError:
            continue
    return total


def estimate_bind_size(containers: list[Container]) -> int:
    seen: set[str] = set()
    total = 0
    for container in containers:
        for mount in container.mounts:
            if mount.type != "bind" or not mount.source or mount.source in seen:
                continue
            seen.add(mount.source)
            total += path_size(Path(mount.source))
    return total


def available_space(path: Path) -> int:
    probe = path.expanduser()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def database_hints(containers: list[Container]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for container in containers:
        image = container.image.lower()
        for keyword, hint in DB_IMAGE_KEYWORDS.items():
            if keyword in image:
                hints[container.name] = hint
                break
    return hints


def ensure_backup_dir(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.name.startswith("docker-backup-"):
        raise ValueError(f"not a docker-backup directory: {candidate}")
    if not (candidate / "manifest.json").is_file():
        raise ValueError(f"manifest.json not found in: {candidate}")
    return candidate
