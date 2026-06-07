"""
Copyright 2025-2026 Lisardo Prieto <me@lisardoprieto.com>
SPDX-License-Identifier: Apache-2.0

The Docker exception taxonomy: specific volume failures are distinguishable from
a daemon-down condition, while all remain catchable via the DockerError base.
"""

from __future__ import annotations

from docker_client import (
    DockerError,
    DockerUnavailable,
    VolumeConflict,
    VolumeInUse,
    VolumeNotFound,
)


def test_all_are_dockererror_subclasses():
    for exc in (DockerUnavailable, VolumeNotFound, VolumeConflict, VolumeInUse):
        assert issubclass(exc, DockerError)


def test_subtypes_are_distinct():
    # A missing volume must NOT read as "daemon unavailable".
    assert not issubclass(VolumeNotFound, DockerUnavailable)
    assert not issubclass(VolumeInUse, DockerUnavailable)
    assert not issubclass(VolumeConflict, DockerUnavailable)


def test_base_catches_every_subtype():
    for exc in (
        DockerUnavailable("down"),
        VolumeNotFound("missing"),
        VolumeConflict("exists"),
        VolumeInUse("busy"),
    ):
        try:
            raise exc
        except DockerError as caught:
            assert caught is exc
