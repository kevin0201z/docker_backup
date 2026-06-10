"""核心工具函数的回归测试。"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from docker_backup.checksums import verify_checksums, write_checksums
from docker_backup.cli import parse_selection
from docker_backup.docker_ops import safe_extract_tar
from docker_backup.restore import restore_backup
from docker_backup.tui import format_backup_time


class CoreTests(unittest.TestCase):
    def test_parse_selection_supports_ranges(self) -> None:
        self.assertEqual(parse_selection("1,3-4", 5), [0, 2, 3])
        self.assertEqual(parse_selection("all", 3), [0, 1, 2])

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

            report = restore_backup(backup, lambda _msg: None, restore_images=False, restore_volumes=False, restore_binds=True)
            self.assertEqual((target / "restored.txt").read_text(encoding="utf-8").strip(), "restored")
            self.assertFalse((target / "old.txt").exists())
            self.assertTrue(any((backup / "restore-safety").glob("*")))
            self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
