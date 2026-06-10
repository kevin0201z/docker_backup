"""备份归档校验和生成与验证。"""

from __future__ import annotations

import hashlib
from pathlib import Path


CHECKSUM_FILE = "checksums.txt"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_targets(backup_dir: Path) -> list[Path]:
    targets: list[Path] = []
    for folder in ["volumes", "binds", "images", "compose", "inspect"]:
        base = backup_dir / folder
        if not base.exists():
            continue
        targets.extend(path for path in base.rglob("*") if path.is_file())
    manifest = backup_dir / "manifest.json"
    if manifest.is_file():
        targets.append(manifest)
    return sorted(targets)


def write_checksums(backup_dir: Path) -> Path:
    checksum_path = backup_dir / CHECKSUM_FILE
    lines = []
    for path in checksum_targets(backup_dir):
        rel = path.relative_to(backup_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    checksum_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return checksum_path


def verify_checksums(backup_dir: Path) -> dict[str, list[str]]:
    backup_root = backup_dir.resolve()
    checksum_path = backup_dir / CHECKSUM_FILE
    result = {"ok": [], "missing": [], "failed": []}
    if not checksum_path.is_file():
        result["missing"].append(CHECKSUM_FILE)
        return result
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split(maxsplit=1)
        rel = rel.strip()
        candidate = Path(rel)
        if candidate.is_absolute():
            result["failed"].append(rel)
            continue
        path = backup_dir / candidate
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(backup_root)
        except ValueError:
            result["failed"].append(rel)
            continue
        if path.is_symlink():
            result["failed"].append(rel)
            continue
        if not path.is_file():
            result["missing"].append(rel)
            continue
        actual = sha256_file(path)
        if actual == expected:
            result["ok"].append(rel)
        else:
            result["failed"].append(rel)
    return result
