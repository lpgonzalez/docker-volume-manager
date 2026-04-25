"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0
"""

from datetime import datetime


def get_formatted_time(format: str = "%Y-%m-%d %H:%M:%S"):
    return datetime.now().strftime(format)
