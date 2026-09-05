"""The SINGLE Python-side source of the Python kernel version (doc 38 SS1:
`sdkSha` includes `kernelVersion`; SS3.4: a kernel change is a version input,
never invisible drift). Bump on ANY behavioral change to a vendored kernel
file. The TS-side `version.ts` mirrors this constant; the `py-kernel-manifest`
determinism gate pins the two byte-for-byte, so drift is a red build.

Vendored kernel file -- imports nothing (self-contained by construction).
"""

from __future__ import annotations

from typing import Final

KERNEL_VERSION: Final[str] = "0.1.2"
"""Semver of the hand-written Python runtime kernel."""

KERNEL_NAME: Final[str] = "doctorine-py-kernel"
"""The kernel's User-Agent product token."""


def user_agent(sdk_name: str, sdk_version: str) -> str:
    """The User-Agent value stamped on every request:
    ``<sdk>/<version> doctorine-py-kernel/<kernelVersion>``.
    """
    return f"{sdk_name}/{sdk_version} {KERNEL_NAME}/{KERNEL_VERSION}"
