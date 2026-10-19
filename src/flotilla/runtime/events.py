"""Best-effort diagnostics never run on the resource maintenance loop."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from flotilla.runtime.types import RuntimeEvent


class EventDispatcher:
    def __init__(self, callback: Callable[[RuntimeEvent], None]) -> None:
        self.callback = callback
        self.queue: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=1024)
        self.closed = threading.Event()
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, name="flotilla-events", daemon=True)
        self.thread.start()

    def emit(self, event: RuntimeEvent) -> None:
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self.closed.set()

    def _run(self) -> None:
        while not self.closed.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.callback(event)
            except Exception:
                logging.getLogger(__name__).warning("runtime event callback failed")
