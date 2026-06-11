#!/usr/bin/env python3
"""Build a single-file zipapp release for docker-backup."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import tempfile
import zipapp
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PROJECT_ROOT / "docker_backup"
DEFAULT_OUTPUT = PROJECT_ROOT / "dist" / "docker-backup.pyz"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(output: Path) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="docker-backup-release-") as tmp:
        staging = Path(tmp)
        shutil.copytree(
            PACKAGE_DIR,
            staging / "docker_backup",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        (staging / "__main__.py").write_text(
            "from docker_backup.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )

        if output.exists():
            output.unlink()
        zipapp.create_archive(staging, output, interpreter="/usr/bin/env python3", compressed=True)

    current_mode = output.stat().st_mode
    output.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    checksum_path = output.parent / "SHA256SUMS"
    checksum_path.write_text(f"{sha256_file(output)}  {output.name}\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the docker-backup single-file release")
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output zipapp path, default: dist/docker-backup.pyz",
    )
    args = parser.parse_args()

    output = build_release(Path(args.output))
    checksum_path = output.parent / "SHA256SUMS"
    print(f"release: {output}")
    print(f"sha256:  {checksum_path}")
    print(f"run:     {output} --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
