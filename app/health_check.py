"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

# Temporary in-memory file to store the app status
STATUS_FILE = "/dev/shm/app_status.txt"


def check_health():
    try:
        with open(STATUS_FILE, "r") as f:
            status = f.read().strip()
        if status == "healthy":
            exit(0)
        else:
            exit(1)
    except FileNotFoundError:
        exit(1)


if __name__ == "__main__":
    check_health()
