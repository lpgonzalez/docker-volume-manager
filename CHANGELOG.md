# Changelog

All notable changes to Docker Volume Manager.

The project follows a [pragmatic, single-developer cadence](https://keepachangelog.com/en/1.1.0/) — no formal release tags yet; dates indicate when the change landed in main.

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
