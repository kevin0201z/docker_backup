# Repository Guidelines

## Project Structure & Module Organization

This repository contains a zero-dependency Docker backup helper.

- `scripts/docker-backup.py` is the main executable Python script. It contains CLI parsing, Docker inspection, backup creation, manifest writing, and restore metadata generation.
- `README.md` documents user-facing behavior, backup contents, restore flow, and common command examples.
- Runtime backup output is written to `backups/docker-backup-YYYYmmdd-HHMMSS/` by default. Treat this as generated data and do not commit it.
- There is currently no dedicated test directory or packaged Python module layout.

## Build, Test, and Development Commands

- `python3 scripts/docker-backup.py --help` shows the available CLI commands and options.
- `python3 scripts/docker-backup.py list` lists Docker containers and detected mounts. Requires access to a running Docker daemon.
- `python3 scripts/docker-backup.py backup` starts an interactive backup.
- `python3 scripts/docker-backup.py backup --non-interactive --containers all --include-volumes --include-binds` runs a full data backup without prompts.
- `python3 -m py_compile scripts/docker-backup.py` performs a quick syntax check without requiring Docker.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints, and standard-library modules unless a dependency is clearly justified. Keep functions small and focused around one behavior, as in `load_containers`, `compose_files`, and `write_manifest`. Use `snake_case` for functions, variables, and CLI option destinations. Prefer `Path` objects for filesystem work and list-style `subprocess` arguments instead of shell strings.

## Testing Guidelines

No automated test suite is currently committed. For code changes, at minimum run `python3 -m py_compile scripts/docker-backup.py`. When Docker behavior changes, verify against disposable containers and volumes before using production data. Exercise both `list` and `backup --non-interactive` paths, and inspect the generated `manifest.json` for expected containers, mounts, compose files, and image archives.

## Commit & Pull Request Guidelines

Git history is not available in this checkout, so use clear, imperative commit messages such as `Add non-interactive backup option` or `Fix compose env file detection`. Pull requests should describe the user-visible behavior change, list manual verification commands, and call out any Docker or filesystem safety implications. Include sample output or screenshots only when CLI behavior or prompts change.

## Security & Configuration Tips

Backup archives may contain secrets from bind mounts, volumes, compose files, `.env`, and inspect metadata. Do not commit generated backups or manifests. Be careful with broad bind mounts, and prefer testing with temporary Docker resources before handling real application data.
