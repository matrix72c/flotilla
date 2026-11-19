"""Consume-only task lifecycle, over a dataset someone else publishes.

Importing the models and the dataset mapping does not load the scheduler, Harbor or Compose.
Runtime exports stay lazy so CPU-side metadata use does not pull the platform in.
"""

from typing import Any

from .dataset import Dataset, DatasetError, datasets, platform_key
from .models import Artifact, Verification

__all__ = [
    "Artifact",
    "Dataset",
    "DatasetError",
    "NativeAttempt",
    "Verification",
    "VerificationResult",
    "datasets",
    "platform_key",
]


def __getattr__(name: str) -> Any:
    if name in {"NativeAttempt", "VerificationResult"}:
        from .runtime import NativeAttempt, VerificationResult

        return {"NativeAttempt": NativeAttempt, "VerificationResult": VerificationResult}[name]
    raise AttributeError(name)
