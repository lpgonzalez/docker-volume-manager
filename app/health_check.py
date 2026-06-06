"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

import sys

# Temporary in-memory file to store the app status
STATUS_FILE = "/dev/shm/app_status.txt"


def check_health() -> None:
    """Docker HEALTHCHECK probe: exit 0 when status is ``healthy``, else 1.

    Uses ``sys.exit`` rather than the ``site``-injected ``exit`` builtin,
    which is absent under ``python -S`` / frozen builds. Any read error
    (missing file, permissions, …) is treated as unhealthy.
    """
    try:
        with open(STATUS_FILE) as f:
            status = f.read().strip()
    except OSError:
        sys.exit(1)
    sys.exit(0 if status == "healthy" else 1)


if __name__ == "__main__":
    check_health()
