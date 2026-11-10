"""Public portable release protocol and consume-only task lifecycle.

Importing readers and models does not load the scheduler, Harbor, Compose or a
builder. Runtime exports are lazy for CPU metadata and installed helper use.
"""

from .models import (
    PROTOCOL_VERSION,
    Artifact,
    AssetRecord,
    DatasetRow,
    FileEntry,
    ReleaseManifest,
    Task,
    TaskRecord,
    Verification,
)
from .release import (
    BoundTask,
    ReleaseBinding,
    ReleaseBindings,
    ReleaseReader,
    ReleaseWriter,
    platform_key,
    read_dataset,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Artifact",
    "AssetRecord",
    "BoundTask",
    "DatasetRow",
    "FileEntry",
    "NativeAttempt",
    "ReleaseBinding",
    "ReleaseBindings",
    "ReleaseManifest",
    "ReleaseReader",
    "ReleaseWriter",
    "Task",
    "TaskRecord",
    "Verification",
    "VerificationResult",
    "platform_key",
    "read_dataset",
]


def __getattr__(name: str):
    if name in {"NativeAttempt", "VerificationResult"}:
        from .runtime import NativeAttempt, VerificationResult

        return {"NativeAttempt": NativeAttempt, "VerificationResult": VerificationResult}[name]
    raise AttributeError(name)
