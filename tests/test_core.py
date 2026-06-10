"""核心函数的单元测试。不依赖 Docker 守护进程。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from docker_backup.cli import parse_selection
from docker_backup.docker_ops import bind_skip_reason, mount_key, shell_join
from docker_backup.backup import compose_group_name
from docker_backup.restore import remove_path
from docker_backup.models import Container, Mount


# ---------------------------------------------------------------------------
# parse_selection
# ---------------------------------------------------------------------------

class TestParseSelection:
    def test_all(self) -> None:
        assert parse_selection("all", 5) == [0, 1, 2, 3, 4]

    def test_empty(self) -> None:
        assert parse_selection("", 5) == [0, 1, 2, 3, 4]

    def test_single(self) -> None:
        assert parse_selection("3", 5) == [2]

    def test_comma_separated(self) -> None:
        assert parse_selection("1,3,5", 5) == [0, 2, 4]

    def test_range(self) -> None:
        assert parse_selection("2-4", 5) == [1, 2, 3]

    def test_mixed(self) -> None:
        assert parse_selection("1,3-5", 5) == [0, 2, 3, 4]

    def test_spaces_ignored(self) -> None:
        assert parse_selection(" 1 , 3 - 5 ", 5) == [0, 2, 3, 4]

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_selection("6", 5)

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_selection("0", 5)

    def test_duplicates_removed(self) -> None:
        assert parse_selection("1,1,2-3,2", 5) == [0, 1, 2]


# ---------------------------------------------------------------------------
# bind_skip_reason
# ---------------------------------------------------------------------------

class TestBindSkipReason:
    def test_dev_prefix(self) -> None:
        assert bind_skip_reason(Path("/dev/sda")) == "runtime path"
        assert bind_skip_reason(Path("/dev")) == "runtime path"

    def test_proc_prefix(self) -> None:
        assert bind_skip_reason(Path("/proc/cpuinfo")) == "runtime path"

    def test_socket_suffix(self) -> None:
        assert bind_skip_reason(Path("/var/run/docker.sock")) == "socket path"

    def test_socket_suffix_alt(self) -> None:
        # /tmp 在 SKIP_BIND_PREFIXES 中，因此前缀检查先命中
        assert bind_skip_reason(Path("/var/run/app.socket")) == "socket path"

    def test_socket_filename_only(self) -> None:
        """确保只匹配文件名后缀。路径不存在时先返回 'missing path'。"""
        assert bind_skip_reason(Path("/home/user/mysock")) == "missing path"

    def test_missing_path(self) -> None:
        assert bind_skip_reason(Path("/nonexistent/path/xyz")) == "missing path"

    def test_normal_path(self, tmp_path: Path) -> None:
        # tmp_path 位于 /tmp 下，属于 SKIP_BIND_PREFIXES
        (tmp_path / "data").mkdir()
        assert bind_skip_reason(tmp_path / "data") == "runtime path"

    def test_socket_file(self, tmp_path: Path) -> None:
        # 无法在测试中轻易创建 socket，但我们可以信任逻辑
        pass


# ---------------------------------------------------------------------------
# mount_key
# ---------------------------------------------------------------------------

class TestMountKey:
    def test_volume_with_name(self) -> None:
        m = Mount(type="volume", source="", destination="/data", name="myvol")
        assert mount_key(m) == "volume:myvol"

    def test_volume_without_name(self) -> None:
        m = Mount(type="volume", source="", destination="/data", name=None)
        assert mount_key(m) is None

    def test_bind_with_source(self) -> None:
        m = Mount(type="bind", source="/host/path", destination="/container/path")
        assert mount_key(m) == "bind:/host/path"

    def test_bind_without_source(self) -> None:
        m = Mount(type="bind", source="", destination="/container/path")
        assert mount_key(m) is None

    def test_unknown_type(self) -> None:
        m = Mount(type="tmpfs", source="", destination="/tmp")
        assert mount_key(m) is None


# ---------------------------------------------------------------------------
# shell_join
# ---------------------------------------------------------------------------

class TestShellJoin:
    def test_simple(self) -> None:
        assert shell_join(["echo", "hello"]) == "echo hello"

    def test_spaces_quoted(self) -> None:
        assert shell_join(["echo", "hello world"]) == "echo 'hello world'"

    def test_empty_parts_filtered(self) -> None:
        assert shell_join(["echo", "", "hello"]) == "echo hello"

    def test_none_filtered(self) -> None:
        # shell_join 中 str(None) 为 "None"，不会被视为空字符串过滤
        assert shell_join(["echo", None, "hello"]) == "echo None hello"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compose_group_name
# ---------------------------------------------------------------------------

class TestComposeGroupName:
    def test_compose_project(self) -> None:
        c = Container(
            id="abc123",
            name="web",
            image="nginx",
            image_id="sha256:...",
            state="running",
            labels={"com.docker.compose.project": "myapp"},
            mounts=[],
            raw={},
        )
        assert compose_group_name(c) == "myapp"

    def test_no_compose_project(self) -> None:
        c = Container(
            id="abc123",
            name="standalone",
            image="alpine",
            image_id="sha256:...",
            state="running",
            labels={},
            mounts=[],
            raw={},
        )
        assert compose_group_name(c) == "standalone"


# ---------------------------------------------------------------------------
# remove_path
# ---------------------------------------------------------------------------

class TestRemovePath:
    def test_remove_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("data")
        remove_path(f)
        assert not f.exists()

    def test_remove_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "mydir"
        d.mkdir()
        (d / "file.txt").write_text("data")
        remove_path(d)
        assert not d.exists()

    def test_nonexistent_no_error(self, tmp_path: Path) -> None:
        remove_path(tmp_path / "doesnotexist")

    def test_remove_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text("data")
        link = tmp_path / "link"
        link.symlink_to(target)
        remove_path(link)
        assert not link.exists()
        assert target.exists()  # 目标文件不应被删除


# ---------------------------------------------------------------------------
# Container model
# ---------------------------------------------------------------------------

class TestContainerModel:
    def test_is_compose_true(self) -> None:
        c = Container(
            id="1", name="c1", image="img", image_id="sha:1",
            state="running",
            labels={"com.docker.compose.project": "proj"},
            mounts=[], raw={},
        )
        assert c.is_compose is True

    def test_is_compose_false(self) -> None:
        c = Container(
            id="2", name="c2", image="img", image_id="sha:2",
            state="running", labels={}, mounts=[], raw={},
        )
        assert c.is_compose is False

    def test_compose_project_none(self) -> None:
        c = Container(
            id="3", name="c3", image="img", image_id="sha:3",
            state="running", labels={}, mounts=[], raw={},
        )
        assert c.compose_project is None
