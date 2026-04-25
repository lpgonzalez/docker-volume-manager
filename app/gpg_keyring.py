"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

"""
Ephemeral GPG keyring helpers — let users pass `--recipient-key-file alice.asc`
without having to pre-import keys into ~/.gnupg.

`setup_recipient_keyring(paths)` builds a fresh GNUPGHOME with the supplied
public-key files imported and `trust-model always` enabled, returning the
homedir + the fingerprints of every imported key. The caller is responsible
for setting `GNUPGHOME` to that homedir for the duration of the gpg pipeline,
and for removing the directory afterwards (use `tear_down_keyring`).
"""

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from typing import List, Tuple

logger = logging.getLogger("dvm")


class KeyringError(RuntimeError):
    """Raised when a public key file cannot be imported or fingerprinted."""


def setup_recipient_keyring(paths: List[str]) -> Tuple[str, List[str]]:
    """
    Create a throwaway GNUPGHOME and import the given public key files.

    Returns (homedir, fingerprints). Cleanup is the caller's responsibility.
    """
    if not paths:
        raise KeyringError("No public key files provided")

    for p in paths:
        if not os.path.isfile(p):
            raise KeyringError(f"Public key file not found: {p}")

    homedir = tempfile.mkdtemp(prefix="dvm-gpg-")
    try:
        os.chmod(homedir, stat.S_IRWXU)  # gpg refuses to use a world-readable homedir
        # Trust the imported keys without manual signing — these are throwaway
        # by design and the user explicitly provided them.
        with open(os.path.join(homedir, "gpg.conf"), "w") as f:
            f.write("trust-model always\n")

        for path in paths:
            try:
                subprocess.run(
                    ["gpg", "--homedir", homedir, "--batch", "--import", path],
                    check=True,
                    capture_output=True,
                )
                logger.debug("Imported public key from %s", path)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
                raise KeyringError(
                    f"Failed to import {path!r}: {stderr.strip() or exc.returncode}"
                ) from exc

        result = subprocess.run(
            ["gpg", "--homedir", homedir, "--list-keys", "--with-colons"],
            check=True,
            capture_output=True,
            text=True,
        )
        fingerprints: List[str] = []
        for line in result.stdout.splitlines():
            # gpg --with-colons emits one line per key field; `fpr:` carries the
            # full 40-char fingerprint at column 10.
            if line.startswith("fpr:"):
                fields = line.split(":")
                if len(fields) > 9 and fields[9]:
                    fingerprints.append(fields[9])

        if not fingerprints:
            raise KeyringError(
                f"No keys could be enumerated from imported files: {paths!r}"
            )

        # Some key files include subkeys; deduplicate while preserving order.
        seen = set()
        unique: List[str] = []
        for fp in fingerprints:
            if fp not in seen:
                seen.add(fp)
                unique.append(fp)
        return homedir, unique
    except Exception:
        shutil.rmtree(homedir, ignore_errors=True)
        raise


def tear_down_keyring(homedir: str) -> None:
    """Best-effort removal of a throwaway keyring directory."""
    if not homedir:
        return
    shutil.rmtree(homedir, ignore_errors=True)
