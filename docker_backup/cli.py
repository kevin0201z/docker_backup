"""Docker 备份工具的命令行入口。"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path

from .backup import perform_backup
from .checksums import verify_checksums
from .docker_ops import consistency_scope, load_containers, require_docker
from .models import BackupOptions, Container
from .restore import restore_backup, scan_backup_dirs
from .tui import tui
from .utils import ensure_backup_dir


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


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {text}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("value must be 1 or greater")
    return value


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

    include_volumes = args.include_volumes or (not args.non_interactive and ask_yes_no("Back up named Docker volumes?", True))
    include_binds = args.include_binds or (not args.non_interactive and ask_yes_no("Back up bind-mounted host paths?", True))
    include_images = args.include_images or (not args.non_interactive and ask_yes_no("Save container images?", False))
    stop_scope, shared_mounts = consistency_scope(selected, containers)
    stop_policy = decide_stop_policy(args, stop_scope, shared_mounts)
    backup_dir = perform_backup(
        selected,
        containers,
        BackupOptions(
            output=Path(args.output),
            include_volumes=include_volumes,
            include_binds=include_binds,
            include_images=include_images,
            stop_policy=stop_policy,
            stop_timeout=args.stop_timeout,
        ),
    )
    print(f"Manifest: {backup_dir / 'manifest.json'}")


def list_only(_: argparse.Namespace) -> None:
    require_docker()
    containers = load_containers()
    if containers:
        print_inventory(containers)
    else:
        print("No containers found.")


def restore_cmd(args: argparse.Namespace) -> None:
    require_docker()
    backup_dir = ensure_backup_dir(Path(args.backup_dir))
    report = restore_backup(
        backup_dir,
        restore_images=not args.no_images,
        restore_volumes=not args.no_volumes,
        restore_binds=not args.no_binds,
        force_conflicts=args.force,
    )
    print(f"Restore report: {report}")


def delete_cmd(args: argparse.Namespace) -> None:
    backup_dir = ensure_backup_dir(Path(args.backup_dir))
    if not args.yes and not ask_yes_no(f"Delete backup {backup_dir}?", False):
        print("Canceled.")
        return
    shutil.rmtree(backup_dir)
    print(f"Deleted backup: {backup_dir}")


def prune_cmd(args: argparse.Namespace) -> None:
    root = Path(args.output)
    backups = scan_backup_dirs(root)
    cutoff = None
    if args.days is not None:
        cutoff = dt.datetime.now() - dt.timedelta(days=args.days)

    to_delete: list[Path] = []
    if args.keep is not None and len(backups) > args.keep:
        to_delete.extend(reversed(backups[args.keep:]))
    if cutoff is not None:
        for backup_dir in backups:
            try:
                stamp = backup_dir.name.removeprefix("docker-backup-")
                created = dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if created < cutoff:
                to_delete.append(backup_dir)

    unique = []
    seen = set()
    for backup_dir in to_delete:
        try:
            resolved = ensure_backup_dir(backup_dir)
        except ValueError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)

    if not unique:
        print("No backups to delete.")
        return
    for backup_dir in unique:
        print(f"Will delete: {backup_dir}")
    if not args.yes and not ask_yes_no("Delete listed backups?", False):
        print("Canceled.")
        return
    for backup_dir in unique:
        shutil.rmtree(backup_dir)
        print(f"Deleted: {backup_dir}")


def check_cmd(args: argparse.Namespace) -> None:
    backup_dir = ensure_backup_dir(Path(args.backup_dir))
    result = verify_checksums(backup_dir)
    print(f"OK: {len(result['ok'])}")
    print(f"Missing: {len(result['missing'])}")
    print(f"Failed: {len(result['failed'])}")
    for key in ["missing", "failed"]:
        for rel in result[key]:
            print(f"{key}: {rel}")
    if result["missing"] or result["failed"]:
        raise SystemExit(1)


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

    tui_parser = subparsers.add_parser("tui", help="Open the Chinese terminal UI")
    tui_parser.add_argument("-o", "--output", default="./backups", help="Directory where backups are read and written")
    tui_parser.set_defaults(func=tui)

    restore_parser = subparsers.add_parser("restore", help="Restore a backup directory")
    restore_parser.add_argument("backup_dir", help="Path to docker-backup-* directory")
    restore_parser.add_argument("--no-images", action="store_true", help="Skip restoring images")
    restore_parser.add_argument("--no-volumes", action="store_true", help="Skip restoring Docker volumes")
    restore_parser.add_argument("--no-binds", action="store_true", help="Skip restoring bind mounts")
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Restore even when running containers use target volumes or binds",
    )
    restore_parser.set_defaults(func=restore_cmd)

    delete_parser = subparsers.add_parser("delete", help="Delete one backup directory")
    delete_parser.add_argument("backup_dir", help="Path to docker-backup-* directory")
    delete_parser.add_argument("-y", "--yes", action="store_true", help="Delete without prompting")
    delete_parser.set_defaults(func=delete_cmd)

    prune_parser = subparsers.add_parser("prune", help="Delete old backups by retention policy")
    prune_parser.add_argument("-o", "--output", default="./backups", help="Directory containing backups")
    prune_parser.add_argument("--keep", type=positive_int, help="Keep newest N backups")
    prune_parser.add_argument("--days", type=positive_int, help="Delete backups older than N days")
    prune_parser.add_argument("-y", "--yes", action="store_true", help="Delete without prompting")
    prune_parser.set_defaults(func=prune_cmd)

    check_parser = subparsers.add_parser("check", help="Verify backup checksums")
    check_parser.add_argument("backup_dir", help="Path to docker-backup-* directory")
    check_parser.set_defaults(func=check_cmd)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    args.func(args)
    return 0
