"""备份执行流程与 manifest 写入逻辑。"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

from .docker_ops import (
    bind_skip_reason,
    compose_files,
    consistency_scope,
    copy_if_exists,
    make_tar_from_path,
    reconstruct_run_command,
    save_image,
    backup_named_volume,
    restart_containers,
    stop_running_containers,
)
from .models import BackupOptions, BackupReport, COMPOSE_WORKDIR, Container


def compose_group_name(container: Container) -> str:
    return container.compose_project or container.name


def _build_manifest_dict(
    selected: list[Container],
    volume_files: dict[str, str],
    bind_files: dict[str, str],
    image_files: dict[str, str],
    copied_compose_files: dict[str, list[str]],
    stopped_containers: list[Container],
    report: BackupReport,
) -> dict:
    """构建 manifest 字典，供 write_manifest 和 write_partial_manifest 复用。"""
    return {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "containers": [
            {
                "name": c.name,
                "id": c.id,
                "image": c.image,
                "state": c.state,
                "compose_project": c.compose_project,
                "compose_service": c.compose_service,
                "compose_workdir": c.labels.get(COMPOSE_WORKDIR),
                "run_command": reconstruct_run_command(c),
                "mounts": [m.__dict__ for m in c.mounts],
                "compose_files": copied_compose_files.get(compose_group_name(c), []),
            }
            for c in selected
        ],
        "volume_archives": volume_files,
        "bind_archives": bind_files,
        "image_archives": image_files,
        "stopped_containers": [c.name for c in stopped_containers],
        "shared_mounts": report.shared_mounts,
        "skipped_binds": report.skipped_binds,
        "failed_binds": report.failed_binds,
        "failed_volumes": report.failed_volumes,
        "restore_notes": [
            "For compose containers, restore bind/volume data first, then run docker compose up -d from the copied compose directory.",
            "For docker-run containers, restore named volumes/bind paths first, load saved images if needed, then use the stored run_command as a starting point.",
            "Inspect metadata is saved under inspect/ for exact original Docker configuration.",
        ],
    }


def write_manifest(
    backup_dir: Path,
    selected: list[Container],
    volume_files: dict[str, str],
    bind_files: dict[str, str],
    image_files: dict[str, str],
    copied_compose_files: dict[str, list[str]],
    stopped_containers: list[Container],
    report: BackupReport,
) -> None:
    manifest = _build_manifest_dict(selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report)
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_partial_manifest(
    backup_dir: Path,
    selected: list[Container],
    volume_files: dict[str, str],
    bind_files: dict[str, str],
    image_files: dict[str, str],
    copied_compose_files: dict[str, list[str]],
    stopped_containers: list[Container],
    report: BackupReport,
    error: Exception,
) -> None:
    """仅在内存中构建并写入 partial manifest，不产生冗余的 manifest.json 磁盘写入。"""
    manifest = _build_manifest_dict(selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report)
    manifest["status"] = "failed"
    manifest["error"] = str(error)
    (backup_dir / "manifest.partial.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def perform_backup(
    selected: list[Container],
    all_containers: list[Container],
    options: BackupOptions,
    log: Callable[[str], None] = print,
) -> Path:
    backup_root = options.output.expanduser()
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"docker-backup-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for child in ["inspect", "compose", "volumes", "binds", "images"]:
        (backup_dir / child).mkdir()

    stop_scope, shared_mounts = consistency_scope(selected, all_containers)
    volume_files: dict[str, str] = {}
    bind_files: dict[str, str] = {}
    image_files: dict[str, str] = {}
    copied_compose_files: dict[str, list[str]] = {}
    stopped_containers: list[Container] = []
    report = BackupReport.empty()
    report.shared_mounts = shared_mounts

    try:
        copied_compose_groups: set[str] = set()
        for c in selected:
            inspect_path = backup_dir / "inspect" / f"{c.name}.json"
            inspect_path.write_text(json.dumps(c.raw, indent=2), encoding="utf-8")

            if c.is_compose:
                group = compose_group_name(c)
                if group not in copied_compose_groups:
                    copied_compose_groups.add(group)
                    container_compose_dir = backup_dir / "compose" / group
                    container_compose_dir.mkdir(exist_ok=True)
                    copied: list[str] = []
                    for file_path in compose_files(c):
                        copied_file = copy_if_exists(file_path, container_compose_dir)
                        if copied_file:
                            copied.append(str(copied_file.relative_to(backup_dir)))
                    copied_compose_files[group] = copied

            if options.include_images and c.image not in image_files:
                safe_name = c.image.replace("/", "_").replace(":", "__")
                out = backup_dir / "images" / f"{safe_name}.tar"
                log(f"正在备份镜像 {c.image}...")
                save_image(c.image, out)
                image_files[c.image] = str(out.relative_to(backup_dir))

        if options.stop_policy == "always":
            stopped_containers = stop_running_containers(stop_scope, options.stop_timeout, log)

        for mount in (m for c in selected for m in c.mounts):
            if options.include_volumes and mount.type == "volume" and mount.name and mount.name not in volume_files:
                out = backup_dir / "volumes" / f"{mount.name}.tar.gz"
                log(f"正在备份数据卷 {mount.name}...")
                try:
                    backup_named_volume(mount.name, out)
                    volume_files[mount.name] = str(out.relative_to(backup_dir))
                except subprocess.CalledProcessError as exc:
                    report.failed_volumes[mount.name] = str(exc)
                    log(f"备份数据卷 {mount.name} 失败：{exc}")

            if options.include_binds and mount.type == "bind" and mount.source and mount.source not in bind_files:
                src = Path(mount.source)
                reason = bind_skip_reason(src)
                if reason:
                    report.skipped_binds[mount.source] = reason
                    log(f"跳过挂载目录 {mount.source}：{reason}")
                    continue
                safe_name = mount.source.strip("/").replace("/", "__") or "root"
                out = backup_dir / "binds" / f"{safe_name}.tar.gz"
                log(f"正在备份挂载目录 {mount.source}...")
                try:
                    make_tar_from_path(src, out)
                    bind_files[mount.source] = str(out.relative_to(backup_dir))
                except (OSError, tarfile.TarError) as exc:
                    report.failed_binds[mount.source] = str(exc)
                    log(f"备份挂载目录 {mount.source} 失败：{exc}")

        write_manifest(backup_dir, selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report)
    except KeyboardInterrupt:
        log("备份被用户中断。")
        raise
    except Exception as exc:
        write_partial_manifest(backup_dir, selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report, exc)
        raise
    finally:
        if stopped_containers:
            restart_containers(stopped_containers, log)

    log(f"备份已完成：{backup_dir}")
    return backup_dir
