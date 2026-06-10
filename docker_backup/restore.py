"""镜像归档、Docker 数据卷和挂载目录的还原流程。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

from .checksums import verify_checksums
from .docker_ops import (
    backup_named_volume,
    clear_volume,
    docker_volume_exists,
    load_containers,
    make_tar_from_path,
    run,
    safe_extract_tar,
)


def scan_backup_dirs(root: Path) -> list[Path]:
    root = root.expanduser()
    if not root.exists():
        return []
    backups = [p for p in root.glob("docker-backup-*") if (p / "manifest.json").is_file()]
    return sorted(backups, key=lambda p: p.name, reverse=True)


def load_manifest(backup_dir: Path) -> dict:
    manifest_path = backup_dir / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def backup_existing_volume(volume: str, safety_dir: Path, log: Callable[[str], None]) -> str | None:
    if not docker_volume_exists(volume):
        return None
    safety_dir.mkdir(parents=True, exist_ok=True)
    out = safety_dir / f"{volume}.tar.gz"
    log(f"正在为已有数据卷创建安全备份：{volume}")
    backup_named_volume(volume, out)
    return str(out)


def restore_volume_archive(volume: str, archive: Path) -> None:
    if not docker_volume_exists(volume):
        run(["docker", "volume", "create", volume], capture=False)
    clear_volume(volume)
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            "-v",
            f"{archive.parent.resolve()}:/backup:ro",
            "alpine",
            "tar",
            "-xzf",
            f"/backup/{archive.name}",
            "-C",
            "/data",
        ],
        capture=False,
    )


def backup_existing_bind(path: Path, safety_dir: Path, log: Callable[[str], None]) -> str | None:
    if not path.exists():
        return None
    safety_dir.mkdir(parents=True, exist_ok=True)
    safe_name = str(path).strip("/").replace("/", "__") or "root"
    out = safety_dir / f"{safe_name}.tar.gz"
    log(f"正在为已有挂载目录创建安全备份：{path}")
    make_tar_from_path(path, out)
    return str(out)


def remove_path(path: Path) -> None:
    """安全删除路径，使用 EAFP 风格避免 TOCTOU 竞态条件。"""
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def restore_bind_archive(target: Path, archive: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    remove_path(target)
    safe_extract_tar(archive, target.parent)


def restore_conflicts(manifest: dict) -> dict[str, list[str]]:
    conflicts: dict[str, list[str]] = {}
    containers = load_containers()
    if not containers:
        return conflicts
    volumes = set(manifest.get("volume_archives", {}).keys())
    binds = set(manifest.get("bind_archives", {}).keys())
    for container in containers:
        if container.state != "running":
            continue
        for mount in container.mounts:
            if mount.type == "volume" and mount.name in volumes:
                conflicts.setdefault(f"volume:{mount.name}", []).append(container.name)
            if mount.type == "bind" and mount.source in binds:
                conflicts.setdefault(f"bind:{mount.source}", []).append(container.name)
    return conflicts


def restore_backup(
    backup_dir: Path,
    log: Callable[[str], None] = print,
    restore_images: bool = True,
    restore_volumes: bool = True,
    restore_binds: bool = True,
) -> Path:
    manifest = load_manifest(backup_dir)
    checksum_result = verify_checksums(backup_dir)
    if checksum_result["failed"]:
        raise RuntimeError(f"checksum verification failed: {', '.join(checksum_result['failed'])}")
    if checksum_result["missing"] == ["checksums.txt"]:
        log("提示：未找到 checksums.txt，跳过归档校验。")
    elif checksum_result["missing"]:
        raise RuntimeError(f"checksum files missing: {', '.join(checksum_result['missing'])}")

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safety_root = backup_dir / "restore-safety" / timestamp
    report = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backup_dir": str(backup_dir),
        "safety_root": str(safety_root),
        "restored_images": {},
        "restored_volumes": {},
        "restored_binds": {},
        "failed": {},
        "conflicts": {},
        "container_commands": [
            c.get("run_command")
            for c in manifest.get("containers", [])
            if c.get("run_command")
        ],
    }
    conflicts = restore_conflicts(manifest)
    report["conflicts"] = conflicts
    for target, containers in conflicts.items():
        log(f"警告：{target} 正被运行中的容器使用：{', '.join(containers)}")

    if restore_images:
        for image, rel_path in manifest.get("image_archives", {}).items():
            archive = backup_dir / rel_path
            log(f"正在恢复镜像：{image}")
            try:
                run(["docker", "load", "-i", str(archive)], capture=False)
                report["restored_images"][image] = str(archive)
            except subprocess.CalledProcessError as exc:
                report["failed"][f"image:{image}"] = str(exc)
                log(f"恢复镜像失败：{image}：{exc}")

    if restore_volumes:
        for volume, rel_path in manifest.get("volume_archives", {}).items():
            archive = backup_dir / rel_path
            log(f"正在恢复数据卷：{volume}")
            try:
                if not archive.is_file():
                    raise FileNotFoundError(f"missing archive: {archive}")
                safety = backup_existing_volume(volume, safety_root / "volumes", log)
                restore_volume_archive(volume, archive)
                report["restored_volumes"][volume] = {"archive": str(archive), "safety_backup": safety}
            except (subprocess.CalledProcessError, OSError, FileNotFoundError) as exc:
                report["failed"][f"volume:{volume}"] = str(exc)
                log(f"恢复数据卷失败：{volume}：{exc}")

    if restore_binds:
        for bind_path, rel_path in manifest.get("bind_archives", {}).items():
            target = Path(bind_path)
            archive = backup_dir / rel_path
            log(f"正在恢复挂载目录：{bind_path}")
            try:
                if not archive.is_file():
                    raise FileNotFoundError(f"missing archive: {archive}")
                safety = backup_existing_bind(target, safety_root / "binds", log)
                restore_bind_archive(target, archive)
                report["restored_binds"][bind_path] = {"archive": str(archive), "safety_backup": safety}
            except (OSError, tarfile.TarError, RuntimeError, FileNotFoundError) as exc:
                report["failed"][f"bind:{bind_path}"] = str(exc)
                log(f"恢复挂载目录失败：{bind_path}：{exc}")

    report_path = backup_dir / "restore-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"还原报告已写入：{report_path}")
    return report_path
