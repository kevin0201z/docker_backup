"""Docker、归档、挂载和文件系统相关的底层操作。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Callable, Iterable

from .models import (
    COMPOSE_CONFIG_FILES,
    COMPOSE_ENV_FILES,
    COMPOSE_WORKDIR,
    Container,
    Mount,
    SKIP_BIND_PREFIXES,
    SKIP_BIND_SUFFIXES,
)


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    kwargs = {"text": True, "check": check}
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    return subprocess.run(cmd, **kwargs)


def docker_json(args: list[str]) -> object:
    result = run(["docker", *args])
    if not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def require_docker() -> None:
    if not shutil.which("docker"):
        raise SystemExit("docker command not found. Please install Docker or add it to PATH.")
    try:
        run(["docker", "version", "--format", "{{.Server.Version}}"])
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SystemExit(f"Cannot talk to Docker daemon. {detail}") from exc


def load_containers() -> list[Container]:
    try:
        rows = run(["docker", "ps", "-a", "--format", "{{.ID}}"]).stdout
    except subprocess.CalledProcessError:
        return []
    ids = [line.strip() for line in rows.splitlines()]
    if not ids:
        return []

    try:
        inspected = docker_json(["inspect", *ids])
    except subprocess.CalledProcessError:
        return []
    containers: list[Container] = []
    for item in inspected:
        labels = item.get("Config", {}).get("Labels") or {}
        mounts = [
            Mount(
                type=m.get("Type", ""),
                source=m.get("Source", ""),
                destination=m.get("Destination", ""),
                name=m.get("Name"),
                rw=m.get("RW", True),
            )
            for m in item.get("Mounts", [])
        ]
        containers.append(
            Container(
                id=item["Id"][:12],
                name=item.get("Name", "").lstrip("/"),
                image=item.get("Config", {}).get("Image", ""),
                image_id=item.get("Image", ""),
                state=item.get("State", {}).get("Status", "unknown"),
                labels=labels,
                mounts=mounts,
                raw=item,
            )
        )
    return sorted(containers, key=lambda c: (c.compose_project or "", c.name))


def shell_join(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts if str(p))


def reconstruct_run_command(container: Container) -> str:
    cfg = container.raw.get("Config", {})
    host = container.raw.get("HostConfig", {})
    network = container.raw.get("NetworkSettings", {})
    cmd = ["docker", "run", "-d", "--name", container.name]

    if host.get("RestartPolicy", {}).get("Name"):
        restart = host["RestartPolicy"]["Name"]
        maximum = host["RestartPolicy"].get("MaximumRetryCount", 0)
        if restart == "on-failure" and maximum:
            restart = f"{restart}:{maximum}"
        cmd.extend(["--restart", restart])

    hostname = cfg.get("Hostname")
    if hostname and hostname != container.id:
        cmd.extend(["--hostname", hostname])

    for env in cfg.get("Env") or []:
        cmd.extend(["-e", env])

    for mount in container.mounts:
        if mount.type == "bind":
            mode = "rw" if mount.rw else "ro"
            cmd.extend(["-v", f"{mount.source}:{mount.destination}:{mode}"])
        elif mount.type == "volume" and mount.name:
            mode = "rw" if mount.rw else "ro"
            cmd.extend(["-v", f"{mount.name}:{mount.destination}:{mode}"])

    port_bindings = host.get("PortBindings") or {}
    for container_port, bindings in sorted(port_bindings.items()):
        for binding in bindings or []:
            host_ip = binding.get("HostIp", "")
            host_port = binding.get("HostPort", "")
            published = f"{host_port}:{container_port}" if not host_ip else f"{host_ip}:{host_port}:{container_port}"
            cmd.extend(["-p", published])

    network_mode = host.get("NetworkMode")
    if network_mode and network_mode not in {"default", "bridge"}:
        cmd.extend(["--network", network_mode])
    elif network.get("Networks"):
        names = list(network["Networks"].keys())
        if names and names[0] not in {"bridge", "none", "host"}:
            cmd.extend(["--network", names[0]])

    workdir = cfg.get("WorkingDir")
    if workdir:
        cmd.extend(["-w", workdir])

    user = cfg.get("User")
    if user:
        cmd.extend(["-u", user])

    entrypoint = cfg.get("Entrypoint")
    if entrypoint:
        cmd.extend(["--entrypoint", " ".join(entrypoint) if isinstance(entrypoint, list) else entrypoint])

    cmd.append(container.image)
    configured_cmd = cfg.get("Cmd")
    if configured_cmd:
        cmd.extend(configured_cmd if isinstance(configured_cmd, list) else [configured_cmd])
    return shell_join(cmd)


def bind_skip_reason(path: Path) -> str | None:
    path_text = str(path)
    if any(path_text == prefix or path_text.startswith(f"{prefix}/") for prefix in SKIP_BIND_PREFIXES):
        return "runtime path"
    if path.name.endswith(SKIP_BIND_SUFFIXES):
        return "socket path"
    if not path.exists():
        return "missing path"
    try:
        if path.is_socket():
            return "socket"
        if path.is_fifo():
            return "fifo"
        if path.is_block_device() or path.is_char_device():
            return "device"
    except OSError as exc:
        return f"cannot inspect path: {exc}"
    return None


def mount_key(mount: Mount) -> str | None:
    if mount.type == "volume" and mount.name:
        return f"volume:{mount.name}"
    if mount.type == "bind" and mount.source:
        return f"bind:{mount.source}"
    return None


def find_shared_mount_users(selected: list[Container], all_containers: list[Container]) -> dict[str, list[Container]]:
    selected_ids = {c.id for c in selected}
    selected_keys = {key for c in selected for m in c.mounts if (key := mount_key(m))}
    shared: dict[str, list[Container]] = {}
    for container in all_containers:
        if container.id in selected_ids:
            continue
        for mount in container.mounts:
            key = mount_key(mount)
            if key in selected_keys:
                shared.setdefault(key, []).append(container)
    return shared


def consistency_scope(selected: list[Container], all_containers: list[Container]) -> tuple[list[Container], dict[str, list[str]]]:
    shared = find_shared_mount_users(selected, all_containers)
    extra_running: dict[str, Container] = {}
    for users in shared.values():
        for container in users:
            if container.state == "running":
                extra_running[container.id] = container
    scoped = list(selected) + [c for c in extra_running.values() if c.id not in {s.id for s in selected}]
    report_shared = {key: [c.name for c in users] for key, users in shared.items()}
    return scoped, report_shared


def stop_running_containers(
    selected: list[Container],
    timeout: int,
    log: Callable[[str], None] = print,
) -> list[Container]:
    stopped: list[Container] = []
    for container in [c for c in selected if c.state == "running"]:
        log(f"正在停止容器 {container.name}...")
        run(["docker", "stop", "-t", str(timeout), container.id], capture=False)
        stopped.append(container)
    return stopped


def restart_containers(containers: list[Container], log: Callable[[str], None] = print) -> None:
    for container in containers:
        log(f"正在启动容器 {container.name}...")
        try:
            run(["docker", "start", container.id], capture=False)
        except subprocess.CalledProcessError as exc:
            log(f"启动容器 {container.name} 失败：{exc}")


def copy_if_exists(src: Path, dst_dir: Path) -> Path | None:
    if not src.exists() or not src.is_file():
        return None
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    return dst


def compose_files(container: Container) -> list[Path]:
    labels = container.labels
    workdir = labels.get(COMPOSE_WORKDIR)
    files: list[Path] = []
    for raw in (labels.get(COMPOSE_CONFIG_FILES) or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute() and workdir:
            path = Path(workdir) / path
        files.append(path)
    for raw in (labels.get(COMPOSE_ENV_FILES) or "").split(","):
        raw = raw.strip()
        if raw:
            path = Path(raw)
            if not path.is_absolute() and workdir:
                path = Path(workdir) / path
            files.append(path)
    if workdir:
        env_path = Path(workdir) / ".env"
        if env_path.exists():
            files.append(env_path)
    return sorted(set(files))


def make_tar_from_path(src: Path, out_file: Path) -> None:
    with tarfile.open(out_file, "w:gz") as tar:
        tar.add(src, arcname=src.name)


def safe_extract_tar(archive: Path, destination: Path) -> None:
    """Extract tar while guarding against path traversal via symlinks.

    On Python 3.12+ we use ``tarfile.extractall(filter='data')`` which
    provides built-in protection against path traversal attacks.

    On older Python we extract member-by-member and re-validate each time
    so that a symlink created by a prior member IS followed by
    ``Path.resolve()`` when checking subsequent members.  Directory
    attribute-setting is deferred until after all members are extracted
    (matching ``extractall``'s internal behaviour) to prevent a directory
    with restrictive permissions from blocking its own contents.
    """
    destination = destination.resolve()
    with tarfile.open(archive, "r:*") as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(destination, filter="data")
            return

        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"unsafe tar entry: {member.name}") from exc
            tar.extract(member, destination, set_attrs=False)
        # Defer directory attribute-setting so that restrictive perms
        # (e.g. chmod 0555) don't block files inside that directory.
        for member in members:
            if member.isdir():
                target_path = (destination / member.name)
                if target_path.exists():
                    os.chmod(target_path, stat.S_IMODE(member.mode))
                    os.utime(target_path, (member.mtime, member.mtime))


def backup_named_volume(volume: str, out_file: Path) -> None:
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/data:ro",
            "-v",
            f"{out_file.parent.resolve()}:/backup",
            "alpine",
            "tar",
            "-czf",
            f"/backup/{out_file.name}",
            "-C",
            "/data",
            ".",
        ],
        capture=False,
    )


def save_image(image: str, out_file: Path) -> None:
    run(["docker", "save", "-o", str(out_file), image], capture=False)


def docker_volume_exists(volume: str) -> bool:
    return run(["docker", "volume", "inspect", volume], check=False).returncode == 0


def clear_volume(volume: str) -> None:
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            "alpine",
            "sh",
            "-c",
            "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
        ],
        capture=False,
    )
