"""核心工具函数的回归测试。"""

from __future__ import annotations

import json
import re
import tarfile
import tempfile
import unittest
from argparse import ArgumentTypeError
from pathlib import Path
from unittest import mock

import docker_backup.docker_ops as docker_ops
from docker_backup.checksums import verify_checksums, write_checksums
from docker_backup.cli import parse_selection, positive_int
from docker_backup.docker_ops import copy_if_exists, reconstruct_run_command, safe_extract_tar
from docker_backup.models import Container, Mount
from docker_backup.restore import restore_backup
from docker_backup.tui import format_backup_time


class CoreTests(unittest.TestCase):
    def test_parse_selection_supports_ranges(self) -> None:
        self.assertEqual(parse_selection("1,3-4", 5), [0, 2, 3])
        self.assertEqual(parse_selection("all", 3), [0, 1, 2])

    def test_positive_int_rejects_zero_and_negative_values(self) -> None:
        self.assertEqual(positive_int("12"), 12)
        with self.assertRaises(ArgumentTypeError):
            positive_int("0")
        with self.assertRaises(ArgumentTypeError):
            positive_int("-1")

    def test_safe_extract_blocks_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload.txt"
            payload.write_text("evil", encoding="utf-8")
            archive = root / "evil.tar"
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="../outside.txt")
            with self.assertRaises((RuntimeError, tarfile.TarError)):
                safe_extract_tar(archive, root / "dest")

    def test_safe_extract_legacy_blocks_absolute_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "evil-link.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("target")
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp/outside-target"
                tar.addfile(info)
            with mock.patch.object(docker_ops.sys, "version_info", (3, 11)):
                with self.assertRaises(RuntimeError):
                    safe_extract_tar(archive, root / "dest")

    def test_copy_if_exists_preserves_compose_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            first = project / "one" / "docker-compose.yml"
            second = project / "two" / "docker-compose.yml"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("services: {one: {}}\n", encoding="utf-8")
            second.write_text("services: {two: {}}\n", encoding="utf-8")

            dst = root / "backup-compose"
            copied_first = copy_if_exists(first, dst, project)
            copied_second = copy_if_exists(second, dst, project)

            self.assertEqual(copied_first, dst / "one" / "docker-compose.yml")
            self.assertEqual(copied_second, dst / "two" / "docker-compose.yml")
            self.assertIn("one", copied_first.read_text(encoding="utf-8"))
            self.assertIn("two", copied_second.read_text(encoding="utf-8"))

    def test_reconstruct_run_command_splits_entrypoint_args(self) -> None:
        container = Container(
            id="abcdef123456",
            name="demo",
            image="alpine",
            image_id="",
            state="exited",
            labels={},
            mounts=[],
            raw={
                "Config": {
                    "Image": "alpine",
                    "Entrypoint": ["/bin/sh", "-c"],
                    "Cmd": ["echo hi"],
                },
                "HostConfig": {},
                "NetworkSettings": {},
            },
        )
        command = reconstruct_run_command(container)
        self.assertIn("--entrypoint /bin/sh", command)
        self.assertIn("alpine -c 'echo hi'", command)
        self.assertNotIn("'/bin/sh -c'", command)

    def test_format_backup_time_uses_seconds(self) -> None:
        formatted = format_backup_time("2026-06-10T01:02:03.123456+00:00")
        self.assertRegex(formatted, re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"))

    def test_checksums_detect_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "docker-backup-test"
            (backup / "inspect").mkdir(parents=True)
            target = backup / "inspect" / "container.json"
            target.write_text("before", encoding="utf-8")
            write_checksums(backup)
            self.assertEqual(verify_checksums(backup)["failed"], [])
            target.write_text("after", encoding="utf-8")
            self.assertEqual(verify_checksums(backup)["failed"], ["inspect/container.json"])

    def test_checksums_reject_paths_outside_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "docker-backup-test"
            backup.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (backup / "checksums.txt").write_text("deadbeef  ../outside.txt\n", encoding="utf-8")
            result = verify_checksums(backup)
            self.assertEqual(result["failed"], ["../outside.txt"])

    def test_checksums_reject_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "docker-backup-test"
            backup.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            link = backup / "link.txt"
            link.symlink_to(outside)
            (backup / "checksums.txt").write_text("deadbeef  link.txt\n", encoding="utf-8")
            result = verify_checksums(backup)
            self.assertEqual(result["failed"], ["link.txt"])

    def test_restore_bind_creates_safety_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "docker-backup-test"
            binds = backup / "binds"
            binds.mkdir(parents=True)

            source = root / "source_dir"
            source.mkdir()
            (source / "restored.txt").write_text("restored\n", encoding="utf-8")
            with tarfile.open(binds / "target.tar.gz", "w:gz") as tar:
                tar.add(source, arcname="target")

            target = root / "target"
            target.mkdir()
            (target / "old.txt").write_text("old\n", encoding="utf-8")
            manifest = {
                "created_at": "test",
                "containers": [],
                "image_archives": {},
                "volume_archives": {},
                "bind_archives": {str(target): "binds/target.tar.gz"},
            }
            (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            write_checksums(backup)

            with mock.patch("docker_backup.restore.load_containers", return_value=[]):
                report = restore_backup(backup, lambda _msg: None, restore_images=False, restore_volumes=False, restore_binds=True)
            self.assertEqual((target / "restored.txt").read_text(encoding="utf-8").strip(), "restored")
            self.assertFalse((target / "old.txt").exists())
            self.assertTrue(any((backup / "restore-safety").glob("*")))
            self.assertTrue(report.is_file())

    def test_restore_aborts_when_running_containers_use_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "docker-backup-test"
            volumes = backup / "volumes"
            volumes.mkdir(parents=True)
            (volumes / "dbdata.tar.gz").write_text("placeholder", encoding="utf-8")
            manifest = {
                "created_at": "test",
                "containers": [],
                "image_archives": {},
                "volume_archives": {"dbdata": "volumes/dbdata.tar.gz"},
                "bind_archives": {},
            }
            (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            write_checksums(backup)
            running = Container(
                id="abcdef123456",
                name="db",
                image="postgres",
                image_id="",
                state="running",
                labels={},
                mounts=[Mount(type="volume", source="", destination="/var/lib/postgresql/data", name="dbdata")],
                raw={},
            )

            with mock.patch("docker_backup.restore.load_containers", return_value=[running]):
                with self.assertRaises(RuntimeError):
                    restore_backup(backup, lambda _msg: None)

            report = json.loads((backup / "restore-report.json").read_text(encoding="utf-8"))
            self.assertIn("volume:dbdata", report["conflicts"])
            self.assertIn("conflicts", report["failed"])


if __name__ == "__main__":
    unittest.main()
