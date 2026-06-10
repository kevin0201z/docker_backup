"""用于备份、还原、查看和删除备份的中文 curses 终端界面。"""

from __future__ import annotations

import curses
import datetime as dt
import locale
import shutil
import textwrap
from pathlib import Path
from typing import Iterable

from .backup import perform_backup
from .docker_ops import consistency_scope, load_containers, require_docker
from .models import BackupOptions, Container
from .restore import load_manifest, restore_backup, scan_backup_dirs
from .utils import available_space, database_hints, estimate_bind_size, human_size


def source_label(container: Container) -> str:
    if container.is_compose:
        return f"compose:{container.compose_project}/{container.compose_service}"
    return "docker-run"


def format_backup_time(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        else:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19]


class DockerBackupTUI:
    def __init__(self, stdscr, backup_root: Path):
        self.stdscr = stdscr
        self.backup_root = backup_root
        self.logs: list[str] = []

    def setup(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        curses.use_default_colors()

    def add_text(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        text = text[: max(0, width - x - 1)]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            # 窗口尺寸边界情况下静默忽略，避免因终端大小变化导致崩溃
            pass

    def draw_frame(self, title: str) -> None:
        self.stdscr.erase()
        self.add_text(0, 2, f"Docker 备份助手 - {title}", curses.A_BOLD)
        self.add_text(1, 2, "↑/↓ 移动  空格 选择/切换  回车 确认  q 返回")
        height, width = self.stdscr.getmaxyx()
        self.add_text(height - 1, 2, "提示：中文路径、容器名和 Docker 命令会按原样显示。")
        if width > 4:
            try:
                self.stdscr.hline(2, 0, "-", width)
            except curses.error:
                pass

    def message(self, title: str, lines: Iterable[str]) -> None:
        self.draw_frame(title)
        y = 4
        for line in lines:
            for wrapped in textwrap.wrap(str(line), width=max(20, self.stdscr.getmaxyx()[1] - 4)) or [""]:
                self.add_text(y, 2, wrapped)
                y += 1
        self.add_text(y + 1, 2, "按任意键继续...")
        self.stdscr.refresh()
        self.stdscr.getch()

    def wrap_lines(self, lines: Iterable[str]) -> list[str]:
        width = max(20, self.stdscr.getmaxyx()[1] - 4)
        wrapped_lines: list[str] = []
        for line in lines:
            wrapped_lines.extend(textwrap.wrap(str(line), width=width) or [""])
        return wrapped_lines

    def show_lines(self, title: str, lines: Iterable[str]) -> None:
        offset = 0
        while True:
            wrapped = self.wrap_lines(lines)
            height = self.stdscr.getmaxyx()[0]
            rows = max(1, height - 5)
            max_offset = max(0, len(wrapped) - rows)
            offset = min(offset, max_offset)

            self.draw_frame(title)
            self.add_text(3, 2, "↑/↓ 滚动  PgUp/PgDn 翻页  Home/End 首尾  q 返回")
            for line_no, line in enumerate(wrapped[offset : offset + rows], start=4):
                self.add_text(line_no, 2, line)
            self.add_text(height - 2, 2, f"第 {offset + 1 if wrapped else 0}-{min(offset + rows, len(wrapped))} 行 / 共 {len(wrapped)} 行")
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key in (ord("q"), 27, 10, 13, curses.KEY_ENTER):
                return
            if key == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif key == curses.KEY_DOWN:
                offset = min(max_offset, offset + 1)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - rows)
            elif key == curses.KEY_NPAGE:
                offset = min(max_offset, offset + rows)
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = max_offset

    def log(self, text: str) -> None:
        self.logs.append(text)
        height, width = self.stdscr.getmaxyx()
        self.draw_frame("执行中")
        visible = self.logs[-max(1, height - 6) :]
        for idx, line in enumerate(visible, start=4):
            self.add_text(idx, 2, line[: width - 4])
        self.stdscr.refresh()

    def menu(self, title: str, items: list[str]) -> int | None:
        index = 0
        while True:
            self.draw_frame(title)
            for i, item in enumerate(items):
                attr = curses.A_REVERSE if i == index else 0
                self.add_text(4 + i, 4, item, attr)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if ord("1") <= key <= ord("9"):
                chosen = key - ord("1")
                if chosen < len(items):
                    return chosen
            if key == curses.KEY_UP:
                index = (index - 1) % len(items)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(items)
            elif key in (10, 13, curses.KEY_ENTER):
                return index

    def prompt_input(self, title: str, prompt: str, default: str) -> str:
        curses.curs_set(1)
        curses.echo()
        self.draw_frame(title)
        self.add_text(4, 2, prompt)
        self.add_text(5, 2, f"当前值：{default}")
        self.add_text(7, 2, "输入新值后回车；直接回车保留当前值。")
        self.add_text(9, 2, "> ")
        self.stdscr.refresh()
        value = self.stdscr.getstr(9, 4, 240).decode("utf-8", errors="replace").strip()
        curses.noecho()
        curses.curs_set(0)
        return value or default

    def confirm(self, title: str, lines: Iterable[str], default: bool = False) -> bool:
        index = 0 if default else 1
        choices = ["确认执行", "取消"]
        while True:
            self.draw_frame(title)
            y = 4
            for line in lines:
                for wrapped in textwrap.wrap(str(line), width=max(20, self.stdscr.getmaxyx()[1] - 4)) or [""]:
                    self.add_text(y, 2, wrapped)
                    y += 1
            y += 1
            for i, item in enumerate(choices):
                attr = curses.A_REVERSE if i == index else 0
                self.add_text(y + i, 4, item, attr)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("y"), ord("Y")):
                return True
            if key in (ord("n"), ord("N")):
                return False
            if key in (ord("q"), 27):
                return False
            if key == curses.KEY_UP:
                index = (index - 1) % len(choices)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(choices)
            elif key in (10, 13, curses.KEY_ENTER):
                return index == 0

    def select_containers(self, containers: list[Container]) -> list[Container] | None:
        index = 0
        selected: set[int] = set(range(len(containers)))
        while True:
            self.draw_frame("选择要备份的容器")
            height, width = self.stdscr.getmaxyx()
            self.add_text(3, 2, "默认已全选。空格切换，a 全选，n 全不选，回车继续。")
            rows = max(1, height - 7)
            start = max(0, min(index - rows + 1, len(containers) - rows))
            for line_no, i in enumerate(range(start, min(len(containers), start + rows)), start=4):
                c = containers[i]
                mark = "[x]" if i in selected else "[ ]"
                text = f"{mark} {c.name} | {c.state} | {source_label(c)} | 挂载 {len(c.mounts)} | {c.image}"
                attr = curses.A_REVERSE if i == index else 0
                self.add_text(line_no, 2, text[: width - 4], attr)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if key == curses.KEY_UP:
                index = (index - 1) % len(containers)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(containers)
            elif key == ord(" "):
                if index in selected:
                    selected.remove(index)
                else:
                    selected.add(index)
            elif key == ord("a"):
                selected = set(range(len(containers)))
            elif key == ord("n"):
                selected.clear()
            elif key in (10, 13, curses.KEY_ENTER):
                if selected:
                    return [containers[i] for i in sorted(selected)]
                self.message("请选择容器", ["至少需要选择一个容器才能继续。"])

    def backup_options(self) -> BackupOptions | None:
        include_volumes = True
        include_binds = True
        include_images = False
        stop_running = True
        output = str(self.backup_root)
        index = 0
        while True:
            items = [
                f"{'[x]' if include_volumes else '[ ]'} 备份数据卷",
                f"{'[x]' if include_binds else '[ ]'} 备份挂载目录",
                f"{'[x]' if include_images else '[ ]'} 备份镜像",
                f"{'[x]' if stop_running else '[ ]'} 备份前停止运行中的容器",
                f"输出目录：{output}",
                "开始备份",
            ]
            self.draw_frame("备份选项")
            self.add_text(3, 2, "数据一致性优先：建议保持“备份前停止运行中的容器”为开启。")
            for i, item in enumerate(items):
                attr = curses.A_REVERSE if i == index else 0
                self.add_text(5 + i, 4, item, attr)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if key == curses.KEY_UP:
                index = (index - 1) % len(items)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(items)
            elif key == ord(" ") or key in (10, 13, curses.KEY_ENTER):
                if index == 0:
                    include_volumes = not include_volumes
                elif index == 1:
                    include_binds = not include_binds
                elif index == 2:
                    include_images = not include_images
                elif index == 3:
                    stop_running = not stop_running
                elif index == 4:
                    output = self.prompt_input("输出目录", "请输入备份输出目录", output)
                elif index == 5:
                    return BackupOptions(
                        output=Path(output),
                        include_volumes=include_volumes,
                        include_binds=include_binds,
                        include_images=include_images,
                        stop_policy="always" if stop_running else "never",
                    )

    def backup_flow(self) -> None:
        try:
            require_docker()
            containers = load_containers()
        except SystemExit as exc:
            self.message("Docker 不可用", [str(exc)])
            return
        if not containers:
            self.message("没有容器", ["当前 Docker 中没有可备份的容器。"])
            return
        selected = self.select_containers(containers)
        if not selected:
            return
        options = self.backup_options()
        if not options:
            return
        running = [c.name for c in consistency_scope(selected, containers)[0] if c.state == "running"]
        lines = [
            f"将备份 {len(selected)} 个容器。",
            f"数据卷：{'是' if options.include_volumes else '否'}；挂载目录：{'是' if options.include_binds else '否'}；镜像：{'是' if options.include_images else '否'}。",
            f"输出目录：{options.output}",
        ]
        if options.include_binds:
            lines.append(f"挂载目录估算大小：{human_size(estimate_bind_size(selected))}")
            lines.append(f"输出目录可用空间：{human_size(available_space(options.output))}")
        hints = database_hints(selected)
        if hints:
            lines.append("")
            lines.append("数据库容器提示：")
            lines.extend(f"- {name}: {hint}" for name, hint in hints.items())
        if options.stop_policy == "always" and running:
            lines.append("将先停止这些运行中的容器，备份完成后自动启动：" + ", ".join(running))
        if not self.confirm("确认备份", lines, default=True):
            return
        self.logs = []
        try:
            backup_dir = perform_backup(selected, containers, options, self.log)
            self.message("备份完成", [f"备份已完成：{backup_dir}", f"索引文件：{backup_dir / 'manifest.json'}"])
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                self.message("备份已取消", ["用户中断了备份操作，已写入部分进度。"])
            else:
                self.message("备份失败", [f"备份过程中发生错误：{exc}", "如果已生成 manifest.partial.json，可用于查看已完成步骤。"])

    def backup_summary_lines(self, backup_dir: Path, manifest: dict) -> list[str]:
        containers = manifest.get("containers", [])
        lines = [
            f"备份目录：{backup_dir}",
            f"备份时间：{format_backup_time(manifest.get('created_at'))}",
            f"容器数量：{len(containers)}",
            f"镜像归档：{len(manifest.get('image_archives', {}))}",
            f"数据卷归档：{len(manifest.get('volume_archives', {}))}",
            f"挂载目录归档：{len(manifest.get('bind_archives', {}))}",
            f"跳过挂载：{len(manifest.get('skipped_binds', {}))}",
            f"失败项：{len(manifest.get('failed_binds', {})) + len(manifest.get('failed_volumes', {}))}",
        ]
        if (backup_dir / "checksums.txt").is_file():
            lines.append("校验文件：checksums.txt")
        if manifest.get("database_hints"):
            lines.append("数据库提示：")
            lines.extend(f"- {name}: {hint}" for name, hint in manifest["database_hints"].items())
        return lines

    def choose_backup(self, title: str, *, oldest_first: bool = False) -> tuple[Path, dict] | None:
        backups = scan_backup_dirs(self.backup_root)
        if oldest_first:
            backups = list(reversed(backups))
        if not backups:
            self.message("没有备份", [f"未在 {self.backup_root} 下找到 docker-backup-*。"])
            return None
        index = 0
        while True:
            self.draw_frame(title)
            rows = max(1, self.stdscr.getmaxyx()[0] - 5)
            start = max(0, min(index - rows + 1, len(backups) - rows))
            for line_no, i in enumerate(range(start, min(len(backups), start + rows)), start=4):
                attr = curses.A_REVERSE if i == index else 0
                self.add_text(line_no, 2, f"{i + 1}/{len(backups)} {backups[i].name}", attr)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if key == curses.KEY_UP:
                index = (index - 1) % len(backups)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(backups)
            elif key in (10, 13, curses.KEY_ENTER):
                try:
                    return backups[index], load_manifest(backups[index])
                except (OSError, ValueError) as exc:
                    self.message("备份不可读", [f"读取备份失败：{exc}"])

    def view_backups(self) -> None:
        choice = self.choose_backup("查看备份")
        if not choice:
            return
        backup_dir, manifest = choice
        lines = self.backup_summary_lines(backup_dir, manifest)
        lines.append("")
        lines.append("容器：")
        for container in manifest.get("containers", []):
            lines.append(f"- {container.get('name')} | {container.get('image')} | {container.get('state')}")
        if manifest.get("skipped_binds"):
            lines.append("")
            lines.append("跳过的挂载目录：")
            for path, reason in manifest["skipped_binds"].items():
                lines.append(f"- {path}：{reason}")
        self.show_lines("备份详情", lines)

    def restore_flow(self) -> None:
        choice = self.choose_backup("选择要还原的备份")
        if not choice:
            return
        backup_dir, manifest = choice
        lines = self.backup_summary_lines(backup_dir, manifest)
        lines.extend(
            [
                "",
                "将直接恢复镜像、数据卷和挂载目录。",
                "覆盖已有数据前会先写入 restore-safety/ 安全备份。",
                "同名容器不会自动替换；完成后会展示/记录重建命令。",
            ]
        )
        if not self.confirm("确认还原", lines, default=False):
            return
        self.logs = []
        try:
            require_docker()
            report_path = restore_backup(backup_dir, self.log)
            commands = [c.get("run_command") for c in manifest.get("containers", []) if c.get("run_command")]
            done_lines = [f"还原完成，报告文件：{report_path}"]
            if commands:
                done_lines.extend(["", "容器重建命令不会自动执行，请按需手动运行：", *commands[:8]])
            self.message("还原完成", done_lines)
        except Exception as exc:
            self.message("还原失败", [f"还原过程中发生错误：{exc}", "请查看 restore-report.json 或终端错误详情。"])

    def delete_backup_flow(self) -> None:
        choice = self.choose_backup("选择要删除的备份", oldest_first=True)
        if not choice:
            return
        backup_dir, manifest = choice
        lines = self.backup_summary_lines(backup_dir, manifest)
        lines.extend(
            [
                "",
                "此操作会永久删除该备份目录，包括 volumes、binds、images、restore-safety 和报告文件。",
                "不会删除 Docker 容器、镜像、数据卷或宿主机业务目录。",
                f"将删除：{backup_dir}",
            ]
        )
        if not self.confirm("确认删除备份", lines, default=False):
            return
        try:
            shutil.rmtree(backup_dir)
            self.message("删除完成", [f"已删除备份：{backup_dir}"])
        except OSError as exc:
            self.message("删除失败", [f"删除备份目录失败：{exc}"])

    def run(self) -> None:
        self.setup()
        while True:
            choice = self.menu("主菜单", ["备份容器", "还原备份", "查看备份", "删除备份", "退出"])
            if choice is None or choice == 4:
                return
            if choice == 0:
                self.backup_flow()
            elif choice == 1:
                self.restore_flow()
            elif choice == 2:
                self.view_backups()
            elif choice == 3:
                self.delete_backup_flow()


def tui(args) -> None:
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda stdscr: DockerBackupTUI(stdscr, Path(args.output)).run())
