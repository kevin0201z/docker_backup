"""Docker 备份工具包。

模块按职责拆分：
- cli: argparse 命令行入口
- tui: 中文 curses 终端界面
- backup: 备份执行和 manifest 写入
- restore: 还原执行和安全备份
- docker_ops: Docker 和文件系统基础操作
- models: 共享常量和数据类
"""
