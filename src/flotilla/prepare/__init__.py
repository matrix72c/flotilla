"""Offline preparation: build images and publish task files once; open consumes the resulting rows."""

from flotilla.prepare.runner import PrepareReport, prepare

__all__ = ["PrepareReport", "prepare"]
