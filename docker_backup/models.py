"""Docker 备份流程共享的常量和数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    raw: dict[str, Any]

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


@dataclass(frozen=True)
class BackupOptions:
    output: Path
    include_volumes: bool
    include_binds: bool
    include_images: bool
    stop_policy: str
    stop_timeout: int = 30
