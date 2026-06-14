# Changelog

All notable changes to Docker Volume Manager.

The project follows a [pragmatic, single-developer cadence](https://keepachangelog.com/en/1.1.0/). Releases are tagged as `vMAJOR.MINOR.PATCH`; pushing a tag publishes the multi-arch image to Docker Hub.

## [4.0.0] — 2026-06-14 — Source/destination = volume, host path, or mounted dir (breaking)

### Added
- **Host-path source/destination**: `--input-host` / `--output-host` (envvars `INPUT_HOST` / `OUTPUT_HOST`) on backup/restore/copy, and `--output-host` on verify. An operation's source or destination can now be an **absolute host directory**, bind-mounted into a helper container by the daemon at run time. Combined with `--input-volume` / `--output-volume`, you can run **socket-only** (no `-v` for the data) — e.g. back up a volume straight to a host directory. `--*-volume` and `--*-host` are mutually exclusive per side; host paths must be absolute.
- **Interactive wizard: location-kind picker.** Each location is chosen as *local* / *volume* / *host* first, then the wizard lists what's there. For host paths it **browses the host filesystem** via helper `find`, seeded from `DVM_HOST_PWD` (passed by `make run-interactive`), with a security confirmation.
- `make run-interactive` now passes `DVM_HOST_PWD` / `DVM_HOST_HOME` so the host browser has a starting point.

### Changed (breaking)
- **In-container mount points moved out of `/app`**: the default source/destination are now **`/dvm/source`** and **`/dvm/dest`** (was `/app/input_dir` / `/app/output_dir`). The env-var *names* (`INPUT_PATH` / `OUTPUT_PATH`) are unchanged — only their defaults moved. Runs that bind-mounted at `/app/input_dir` / `/app/output_dir` must switch to `/dvm/source` / `/dvm/dest` (or pass explicit `-i` / `-o`). The `make run-*` workflow is unaffected (its mounts moved with the defaults).
- **Restore defaults fixed and made symmetric with backup**: restore now reads the backup from `/dvm/dest` (where backup writes) and restores into `/dvm/source`, instead of defaulting the backup *source* to the data-input dir — which produced a spurious "No backups found" in the wizard.
- The helper pivot generalized from volume-only to any *remote* side (`_pivot_if_volumes` → `_pivot_if_remote`); the backup-store abstraction unified into `RemoteStore` (one code path for volumes and host paths).

### Security
- Mounting a host path through the Docker socket grants the helper root-level access to that path on the host. The wizard warns and confirms before accepting one; scripted runs print a one-line notice. Treat socket access as host-equivalent.

### Image metadata
- The publish workflow now pins the curated OCI `description` / `url` and surfaces `authors` / `vendor` / `documentation`, so the published image's label set matches the Dockerfile instead of the auto-generated GitHub values.

## [3.1.0] — 2026-06-14 — Honest verify, typed exit codes, redesigned wizard

### Added
- **`verify --no-repair`**: strictly read-only audit that never modifies the backup. Auto-repair remains the default.
- **`verify -t/--timestamp`**: verify a specific backup timestamp (default: the most recent).
- **`verify.outcome` code** logged on every run: `INTACT` / `REPAIRED` / `CORRUPT_REPAIRABLE` / `CORRUPT_UNREPAIRABLE` / `DAMAGED` / `DECRYPT_FAILED` / `BACKUP_MISSING` …
- **Per-operation exit codes** (tens digit = operation): BACKUP `10-13`, RESTORE `20-25`, VERIFY `30-35`, COPY `40-42`, RENAME `50-53`, VOLUMES `60-62`. Operations raise `OperationError` (or carry a `.code` on `RestoreError`/`RenameError`); the runner maps it to the process exit code.
- **TAB completion in the interactive wizard** (stdlib `readline`): operations, choices, filesystem paths, backup names and volume names. No-op when `readline` is unavailable.
- **Location-first wizard flow**: choose a directory or Docker volume, then pick from a numbered, paginated listing of existing backup names and dated timestamps (newest first). New `BackupStore` abstraction (`LocalDirStore` / `VolumeStore`); volume contents are enumerated via a throwaway helper container. New modules `app/completion.py`, `app/wizard_store.py`, `app/wizard_ui.py`.

### Changed
- **`verify` tells the truth about PAR2 parity.** It classifies the archive instead of silently repairing and always reporting healthy; the health verdict (and exit code) reflect the real outcome. Auto-repair stays the default but logs loudly that it modified the file.
- Wizard prompts reordered to location → list → select, validating an archive is present before continuing; the backup flow checks the destination is writable.
- Generic operation failures still exit `4`; scripts checking `$? != 0` are unaffected, but callers can now branch on the specific code.

## [3.0.1] — 2026-06-13

### Fixed
- **`verify` archive location**: verify now locates the archive in the real `<name>/<timestamp>/` layout (it previously treated `<output>/<name>` — a directory — as the archive and always passed). Bad backups now fail instead of reporting healthy.
- **Wizard restore prompts** clarified: backup *source* (where the backup lives) vs restore *destination* are now distinct and explained.
- Metadata-fidelity tests made robust (file-only mtime checks, ±1s tolerance).

## [3.0.0] — 2026-06-08 — Quality, observability and multi-arch publish

### Added
- **Multi-arch Docker Hub publish** (amd64 + arm64) on tag push; native amd64/arm64 CI matrix; OCI image labels.
- **Live byte-level progress** plus a subprocess **stall watchdog** (`DVM_STALL_TIMEOUT`) that terminates a hung backup/restore.
- **Extended-attribute / POSIX-ACL preservation** (SCHILY.xattr PAX records) and directory mtime fidelity, asserted by tests.
- Ruff (lint + format) and Pyright tooling.

### Changed
- CLI split from the former monolith into focused modules (`cli_shared` / `pivot` / `runners` / `volumes_cli` / `wizard` / `cli`) with a clean, cycle-free dependency direction.
- Compression codecs centralized into a single registry (`operations/codecs.py`).
- Docker errors organized into a typed exception taxonomy; helper-container pivot dispatch centralized.

### Fixed
- Correctness and security bugs surfaced during the refactor (see commit history).

## [2.0.0] — 2026-04-25 — Major overhaul (breaking)

### Added
- `--recipient-key-file` flag for asymmetric encryption from a `.asc` file (no keyring mount needed; imported into a temp keyring with `trust-model always`).
- `--sign-key` / `--sign-key-passphrase` for detached GPG signatures alongside the archive.
- ZSTD compression with `-T0 -19` (parallel, max practical level). Default for new backups.
- Auto-detection of `pigz` for parallel gzip compression in the encrypt pipeline.
- Comprehensive test suite: 178 tests across unit / functional / integration tiers, with auto-marker by directory.
- Interactive wizard menu loop — operations no longer terminate the wizard session; you return to the menu.
- `dvm volumes create` and `dvm volumes remove` (CLI + wizard).
- Docker-volume-aware operations via `--input-volume` / `--output-volume` (helper container pivot with bind-mount inheritance).
- `dvm rename` for atomic Docker volume rename with rollback.
- `app/gpg_keyring.py` for ephemeral GPG homedir management.
- `app/progress.py` with `ProgressReporter` (TTY-adaptive) and `status` (spinner).
- `app/docker_client.py` as a typed facade over docker-py.
- `make test-unit`, `make test-functional`, `make test-integration`, `make test-integration-slow`, `make test-integration-all`, `make coverage`.

### Changed (breaking)
- **Compression options reduced to `NONE` / `GZ` / `ZSTD`**. `LOW` / `MEDIUM` / `HIGH` (gz / bz2 / xz) are no longer valid.
- **bz2 and xz removed entirely**: backup pipeline doesn't produce them; restore and verify don't read them. Migrate legacy archives to ZSTD.
- Default compression is now `ZSTD` (was `MEDIUM` = bz2).
- Base image migrated to `python:3.14-alpine` (was Debian slim). Image size dropped from ~220 MB to ~134 MB.
- CLI rewritten on `typer` + `rich`. Old `argparse`-style invocation gone.
- Configuration centralised in a typed `Config` dataclass; the global `global_config` singleton was removed. Operations get the logger via `logging.getLogger("dvm")`.
- PAR2 layout uses `-n1` (single recovery volume) — backups now produce exactly two `.par2` files instead of seven.
- `tar`/`gpg`/`zstd` etc. invocations made explicit-CLI (no more `OPERATION=BACKUP` env-var dispatch). Subcommand-driven.

### Fixed (real bugs found by tests during development)
- **Restore of encrypted archives**: `_decrypt_gpg_file` created the inner-archive tempfile with a `.tar` suffix regardless of actual compression, so `_determine_tar_mode` returned `"r:"` and Python `tarfile` failed to read compressed content. Now preserves the original extension.
- **`ProgressReporter` over-advance spam**: once `_processed >= total`, every additional `advance()` emitted another log line. Capped — no more spam after 100%.
- **`verify_all` stale `can_decompress`**: the field was evaluated before PAR2 repair, so a corrupted-then-recovered archive reported `can_decompress=False` despite being repaired in-place. Now re-checks decompression after a successful PAR2 recovery.
- **`zstd -T 0` SIGPIPE**: the space-separated flag form was parsed as a filename `0`. Switched to `-T0` (joined) which works on all zstd versions.
- **`gpg_recipients` never propagated**: `Docker_Volume_Manager.backup` read `GPG_RECIPIENTS` but did not pass it to `BackupManager`, silently disabling asymmetric encryption. Fixed.
- **GNUPGHOME cleanup on pivot**: `--recipient-key-file` cleanup now runs in `finally:` so a failing pivot doesn't leak the temp homedir.

### Removed
- Legacy `argparse` CLI in `main.py`.
- `bzip2` / `xz` system packages from the runtime/test images.
- `bzip2-dev` / `xz-dev` from the builder stage.
- `_test_bz2`, `_test_xz`, `_test_bz2_file`, `_test_xz_file` from `BackupVerifier`.
- `bz2`, `lzma` Python stdlib imports from `verify_backup.py`.
- `global_config` singleton from `config.py`.
- Module-level `import dotenv; load_dotenv()` side-effect on first config import (still loaded explicitly via `Config.from_env`).

### Security
- `docker.sock` is mounted only on Make targets that genuinely need it (interactive, volumes management, anything passing `input-volume=`/`output-volume=`).
- `--recipient-key-file` uses a chmod-700 temp homedir and removes it on exit.
- Symmetric passphrase forwarding through the helper pivot uses env vars (not argv) so secrets don't appear in `docker inspect` of the helper container.

## Pre-cleanup baseline

The pre-cleanup version was a Python+argparse tool on a Debian slim base, producing tar archives with gz/bz2/xz/none compression. It worked but had a fragmented config flow (env vars + CLI args mixed across three places), no Docker SDK integration, and no test suite beyond a single 117-line file. None of that is preserved in the current version — see the docstrings for the current architecture.
