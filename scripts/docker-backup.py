#!/usr/bin/env python3
"""Interactive Docker container backup helper.

The tool backs up the pieces that matter for recovery:
- inspect metadata and a best-effort docker run command
- compose files for compose-managed containers, when Docker labels expose them
- named volumes and bind mounts
- selected images via docker save
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_WORKDIR = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES = "com.docker.compose.project.config_files"
COMPOSE_ENV_FILES = "com.docker.compose.project.environment_file"

SKIP_BIND_PREFIXES = ("/dev", "/proc", "/run", "/sys", "/tmp")
SKIP_BIND_SUFFIXES = (".sock", ".socket")


@dataclass(frozen=True)
class Mount:
    type: str
    source: str
    destination: str
    name: str | None = None
    rw: bool = True


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    image_id: str
    state: str
    labels: dict[str, str]
    mounts: list[Mount]
    raw: dict

    @property
    def is_compose(self) -> bool:
        return COMPOSE_PROJECT in self.labels

    @property
    def compose_project(self) -> str | None:
        return self.labels.get(COMPOSE_PROJECT)

    @property
    def compose_service(self) -> str | None:
        return self.labels.get(COMPOSE_SERVICE)


@dataclass
class BackupReport:
    skipped_binds: dict[str, str]
    failed_binds: dict[str, str]
    failed_volumes: dict[str, str]
    shared_mounts: dict[str, list[str]]

    @classmethod
    def empty(cls) -> "BackupReport":
        return cls(skipped_binds={}, failed_binds={}, failed_volumes={}, shared_mounts={})


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    kwargs = {
        "text": True,
        "check": check,
    }
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
    rows = run(["docker", "ps", "-a", "--format", "{{.ID}}"]).stdout
    ids = [line.strip() for line in rows.splitlines()]
    if not ids:
        return []

    inspected = docker_json(["inspect", *ids])
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


def print_inventory(containers: list[Container]) -> None:
    print("\nContainers:")
    for idx, c in enumerate(containers, start=1):
        source = f"compose:{c.compose_project}/{c.compose_service}" if c.is_compose else "docker-run"
        mounts = ", ".join(f"{m.type}:{m.name or m.source}->{m.destination}" for m in c.mounts) or "no mounts"
        print(f"{idx:>2}. {c.name:<28} {c.state:<10} {source:<32} {c.image}")
        print(f"    mounts: {mounts}")


def parse_selection(text: str, count: int) -> list[int]:
    text = text.strip().lower()
    if not text or text == "all":
        return list(range(count))
    selected: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            selected.update(range(int(start) - 1, int(end)))
        else:
            selected.add(int(part) - 1)
    bad = [n + 1 for n in selected if n < 0 or n >= count]
    if bad:
        raise ValueError(f"Selection out of range: {bad}")
    return sorted(selected)


def prompt_selection(containers: list[Container], args: argparse.Namespace) -> list[Container]:
    while True:
        selection_text = args.containers or input("\nSelect containers, e.g. all, 1,3,4-6 [all]: ")
        try:
            return [containers[i] for i in parse_selection(selection_text, len(containers))]
        except ValueError as exc:
            if args.containers or args.non_interactive:
                raise SystemExit(f"Invalid container selection: {exc}") from exc
            print(f"Invalid selection: {exc}")


def ask_yes_no(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def bind_skip_reason(path: Path) -> str | None:
    path_text = str(path)
    if any(path_text == prefix or path_text.startswith(f"{prefix}/") for prefix in SKIP_BIND_PREFIXES):
        return "runtime path"
    if path_text.endswith(SKIP_BIND_SUFFIXES):
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


def decide_stop_policy(args: argparse.Namespace, stop_scope: list[Container], shared_mounts: dict[str, list[str]]) -> str:
    running = [c for c in stop_scope if c.state == "running"]
    if not running or args.stop == "never":
        return "never"
    if args.stop == "always":
        return args.stop
    if args.non_interactive:
        return "never"

    print("\nRunning containers selected:")
    for c in running:
        print(f"- {c.name} ({c.image})")
    if shared_mounts:
        print("\nShared mounts detected with other containers:")
        for key, names in shared_mounts.items():
            print(f"- {key}: {', '.join(names)}")
    if ask_yes_no("Stop running containers before backing up data, then start them again?", True):
        return "always"
    return "never"


def stop_running_containers(selected: list[Container], timeout: int) -> list[Container]:
    to_stop = [c for c in selected if c.state == "running"]
    stopped: list[Container] = []
    for container in to_stop:
        print(f"Stopping {container.name}...")
        run(["docker", "stop", "-t", str(timeout), container.id], capture=False)
        stopped.append(container)
    return stopped


def restart_containers(containers: list[Container]) -> None:
    for container in containers:
        print(f"Starting {container.name}...")
        try:
            run(["docker", "start", container.id], capture=False)
        except subprocess.CalledProcessError as exc:
            print(f"Failed to start {container.name}: {exc}")


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


def compose_group_name(container: Container) -> str:
    return container.compose_project or container.name


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
    manifest = {
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
    error: BaseException,
) -> None:
    write_manifest(backup_dir, selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report)
    manifest_path = backup_dir / "manifest.json"
    partial_path = backup_dir / "manifest.partial.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["status"] = "failed"
    data["error"] = str(error)
    partial_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def backup(args: argparse.Namespace) -> None:
    require_docker()
    containers = load_containers()
    if not containers:
        print("No containers found.")
        return

    print_inventory(containers)
    selected = prompt_selection(containers, args)
    if not selected:
        print("Nothing selected.")
        return

    backup_root = Path(args.output).expanduser()
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"docker-backup-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for child in ["inspect", "compose", "volumes", "binds", "images"]:
        (backup_dir / child).mkdir()

    include_volumes = args.include_volumes or (not args.non_interactive and ask_yes_no("Back up named Docker volumes?", True))
    include_binds = args.include_binds or (not args.non_interactive and ask_yes_no("Back up bind-mounted host paths?", True))
    include_images = args.include_images or (not args.non_interactive and ask_yes_no("Save container images?", False))
    stop_scope, shared_mounts = consistency_scope(selected, containers)
    stop_policy = decide_stop_policy(args, stop_scope, shared_mounts)

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

            if include_images and c.image not in image_files:
                safe_name = c.image.replace("/", "_").replace(":", "__")
                out = backup_dir / "images" / f"{safe_name}.tar"
                print(f"Saving image {c.image}...")
                save_image(c.image, out)
                image_files[c.image] = str(out.relative_to(backup_dir))

        if stop_policy == "always":
            stopped_containers = stop_running_containers(stop_scope, args.stop_timeout)

        all_mounts = [m for c in selected for m in c.mounts]
        for mount in all_mounts:
            if include_volumes and mount.type == "volume" and mount.name and mount.name not in volume_files:
                out = backup_dir / "volumes" / f"{mount.name}.tar.gz"
                print(f"Backing up volume {mount.name}...")
                try:
                    backup_named_volume(mount.name, out)
                    volume_files[mount.name] = str(out.relative_to(backup_dir))
                except subprocess.CalledProcessError as exc:
                    report.failed_volumes[mount.name] = str(exc)
                    print(f"Failed to back up volume {mount.name}: {exc}")

            if include_binds and mount.type == "bind" and mount.source and mount.source not in bind_files:
                src = Path(mount.source)
                reason = bind_skip_reason(src)
                if reason:
                    report.skipped_binds[mount.source] = reason
                    print(f"Skipping bind path {mount.source}: {reason}")
                    continue
                safe_name = mount.source.strip("/").replace("/", "__") or "root"
                out = backup_dir / "binds" / f"{safe_name}.tar.gz"
                print(f"Backing up bind path {mount.source}...")
                try:
                    make_tar_from_path(src, out)
                    bind_files[mount.source] = str(out.relative_to(backup_dir))
                except (OSError, tarfile.TarError) as exc:
                    report.failed_binds[mount.source] = str(exc)
                    print(f"Failed to back up bind path {mount.source}: {exc}")

        write_manifest(backup_dir, selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report)
    except BaseException as exc:
        write_partial_manifest(backup_dir, selected, volume_files, bind_files, image_files, copied_compose_files, stopped_containers, report, exc)
        raise
    finally:
        if stopped_containers:
            restart_containers(stopped_containers)

    print(f"\nBackup completed: {backup_dir}")
    print(f"Manifest: {backup_dir / 'manifest.json'}")


def list_only(_: argparse.Namespace) -> None:
    require_docker()
    containers = load_containers()
    if containers:
        print_inventory(containers)
    else:
        print("No containers found.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive Docker backup helper")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List containers and detected backup sources")
    list_parser.set_defaults(func=list_only)

    backup_parser = subparsers.add_parser("backup", help="Create an interactive backup")
    backup_parser.add_argument("-o", "--output", default="./backups", help="Directory where backups are written")
    backup_parser.add_argument("-c", "--containers", help="Selection such as all, 1,3,4-6")
    backup_parser.add_argument("--include-volumes", action="store_true", help="Back up named Docker volumes")
    backup_parser.add_argument("--include-binds", action="store_true", help="Back up bind-mounted host paths")
    backup_parser.add_argument("--include-images", action="store_true", help="Save selected container images")
    backup_parser.add_argument(
        "--stop",
        choices=["ask", "never", "always"],
        default="ask",
        help="Whether to stop running selected containers before backing up data",
    )
    backup_parser.add_argument("--stop-timeout", type=int, default=30, help="Seconds to wait when stopping containers")
    backup_parser.add_argument("--non-interactive", action="store_true", help="Do not prompt; use only provided flags")
    backup_parser.set_defaults(func=backup)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
