# Docker Backup Helper

一个零依赖的交互式 Docker 备份工具，适合同时存在 `docker run` 容器和 `docker compose` 容器的机器。

它备份的是恢复真正需要的信息：

- 容器 `docker inspect` 元数据
- `docker run` 容器的最佳努力启动命令
- compose 容器的 compose 文件和 `.env`，前提是 Docker labels 能定位到这些文件
- 命名 Docker volume
- bind mount 的宿主机目录
- 可选的镜像 `docker save` 归档

工具优先保证数据一致性：交互模式下选中 running 容器时，默认建议先停止容器，备份完成后再启动回来。无恢复价值的运行时 bind mount 会默认跳过，例如 `/run`、`/proc`、`/sys`、`/dev`、`/tmp` 和 socket/FIFO/device 文件。

## 使用

生成单文件发布版：

```bash
python3 scripts/build-release.py
```

会生成：

```text
dist/docker-backup.pyz
dist/SHA256SUMS
```

把 `dist/docker-backup.pyz` 复制到其他机器后即可执行：

```bash
chmod +x docker-backup.pyz
./docker-backup.pyz --help
./docker-backup.pyz list
```

也可以不用执行权限，直接用 Python 运行：

```bash
python3 docker-backup.pyz --help
```

目标机器需要有 Python 3.9+、Docker CLI，并且当前用户能访问 Docker daemon。发布包仍是零第三方依赖。建议在 Linux/Unix 或 WSL 中运行；中文 TUI 使用标准库 `curses`，Windows 原生 Python 通常不自带该模块。

列出当前容器和挂载：

```bash
python3 scripts/docker-backup.py list
```

交互式备份：

```bash
python3 scripts/docker-backup.py backup
```

打开中文 TUI：

```bash
python3 scripts/docker-backup.py tui
```

校验备份归档：

```bash
python3 scripts/docker-backup.py check backups/docker-backup-YYYYmmdd-HHMMSS
```

命令行还原备份：

```bash
python3 scripts/docker-backup.py restore backups/docker-backup-YYYYmmdd-HHMMSS
```

如果目标 volume 或 bind mount 正被 running 容器使用，还原会默认中止。确认要覆盖时可以显式加 `--force`：

```bash
python3 scripts/docker-backup.py restore backups/docker-backup-YYYYmmdd-HHMMSS --force
```

命令行删除单个备份：

```bash
python3 scripts/docker-backup.py delete backups/docker-backup-YYYYmmdd-HHMMSS
```

按保留策略清理旧备份：

```bash
python3 scripts/docker-backup.py prune --keep 5
python3 scripts/docker-backup.py prune --days 30
```

`--keep` 和 `--days` 必须是大于等于 1 的整数。

TUI 主菜单包含：

- `备份容器`
- `还原备份`
- `查看备份`
- `删除备份`
- `退出`

常用快捷键：

- `↑/↓` 移动
- `空格` 选择或切换选项
- `回车` 确认
- `q` 返回或退出

指定输出目录：

```bash
python3 scripts/docker-backup.py backup -o /path/to/backups
```

非交互式备份全部容器、volume 和 bind mount：

```bash
python3 scripts/docker-backup.py backup \
  --non-interactive \
  --containers all \
  --include-volumes \
  --include-binds
```

如果还想备份镜像：

```bash
python3 scripts/docker-backup.py backup \
  --non-interactive \
  --containers all \
  --include-volumes \
  --include-binds \
  --include-images
```

备份前停止正在运行的容器，备份结束后自动启动回来：

```bash
python3 scripts/docker-backup.py backup \
  --containers all \
  --include-volumes \
  --include-binds \
  --stop always
```

如果选中的容器和其他 running 容器共享同一个 volume 或 bind mount，工具会提示共享关系；使用 `--stop always` 时，会把这些共享挂载的 running 容器也一起停止并在备份后启动回来。

停止容器时默认等待 30 秒，可以调整：

```bash
python3 scripts/docker-backup.py backup --stop always --stop-timeout 60
```

## 备份产物

默认会生成：

```text
backups/docker-backup-YYYYmmdd-HHMMSS/
  manifest.json
  inspect/
  compose/
  volumes/
  binds/
  images/
  checksums.txt
```

`manifest.json` 是恢复索引，里面包含容器、挂载、compose 文件、镜像归档，以及普通容器的 `docker run` 启动命令。

如果备份时选择了停止容器，`manifest.json` 也会记录本次被停止过的容器名称、共享挂载关系、被跳过的 bind mount，以及单项备份失败记录。

如果备份过程中发生整体异常，工具会写出 `manifest.partial.json`，方便判断哪些步骤已经完成。

备份完成后会生成 `checksums.txt`，记录 manifest、inspect、compose、volume、bind、image 归档的 SHA256。还原前如果存在该文件，工具会先校验归档；校验失败会停止还原。

如果检测到 PostgreSQL、MySQL、Redis、MongoDB、Oracle 等常见数据库镜像，确认页和 manifest 会提示建议搭配数据库自身的逻辑备份工具。

## TUI 还原

TUI 会从 `backups/docker-backup-*` 中读取 `manifest.json` 并展示中文详情。

确认还原后，工具会直接执行：

- `docker load` 恢复镜像归档
- 恢复命名 Docker volume
- 恢复 bind mount 目录

为了避免误覆盖，已有 volume 或 bind mount 目标会先备份到：

```text
backups/docker-backup-YYYYmmdd-HHMMSS/restore-safety/<timestamp>/
```

还原过程会写出：

```text
backups/docker-backup-YYYYmmdd-HHMMSS/restore-report.json
```

同名容器不会自动停止、删除或替换。还原完成后，TUI 会展示 `manifest.json` 中记录的 `docker run` 命令或 compose 恢复提示，供你确认后手动执行。

还原前会检查目标 volume 或 bind mount 是否被运行中的容器使用；如果发现冲突，会写入日志和 `restore-report.json` 并默认中止。命令行只有在传入 `--force` 时才会继续覆盖，TUI 会展示冲突并要求先处理运行中的容器。

## TUI 删除备份

`删除备份` 会从 `backups/docker-backup-*` 中按从旧到新的顺序选择一个备份目录，展示摘要并要求二次确认。

删除操作只会删除该备份目录本身，包括其中的 `volumes/`、`binds/`、`images/`、`restore-safety/` 和报告文件；不会删除 Docker 容器、镜像、数据卷或宿主机业务目录。

## 恢复思路

compose 容器：

1. 先恢复 `volumes/` 和 `binds/` 中的数据。
2. 进入 `compose/` 下对应容器目录，检查复制出来的 compose 文件和 `.env`。
3. 使用 `docker compose up -d` 重建服务。

compose 文件会按项目放在 `compose/<project>/` 下，而不是按容器拆散。

`docker run` 容器：

1. 先恢复命名 volume 和 bind mount 目录。
2. 如果备份了镜像，执行 `docker load -i images/xxx.tar`。
3. 参考 `manifest.json` 里的 `run_command` 重建容器。

命名 volume 的恢复示例：

```bash
docker volume create my_volume
docker run --rm \
  -v my_volume:/data \
  -v "$PWD/backups/docker-backup-YYYYmmdd-HHMMSS/volumes:/backup" \
  alpine \
  tar -xzf /backup/my_volume.tar.gz -C /data
```

bind mount 的恢复示例：

```bash
tar -xzf backups/docker-backup-YYYYmmdd-HHMMSS/binds/path__to__data.tar.gz -C /restore/parent
```

## 注意事项

- 数据库类容器最好使用 `--stop always`，或者使用数据库自身的 dump 工具配合这个工具，避免热备文件不一致。
- `--stop always` 会停止本次选中且原本处于 running 状态的容器；如果其他 running 容器共享同一个挂载，也会一起停止，并在备份结束后尽力启动回来。
- `docker export` 不适合作为主要备份方式，因为它不会包含 volume 数据，也不能完整保留启动配置。
- compose 文件依赖 Docker labels 定位；如果原始 compose 文件已经移动或删除，工具仍会保存 `inspect/` 和挂载数据。
- bind mount 目录可能很大，第一次运行前建议先用 `list` 看清楚会备份哪些路径；运行时路径和特殊文件会默认跳过。
