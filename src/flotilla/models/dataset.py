"""Names reserved at the top of the dataset roots on shared storage.

A dataset root holds published task directories and the runtime's own directories; the
reserved names are the runtime's, so a task key that uses one is not a task.
"""

from __future__ import annotations

RESERVED = frozenset({"launches", "share"})
