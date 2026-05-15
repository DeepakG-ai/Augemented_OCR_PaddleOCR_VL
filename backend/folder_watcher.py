"""
folder_watcher.py — watchdog-based PDF folder monitor.

Uses OS-native filesystem events (ReadDirectoryChangesW on Windows,
inotify on Linux, kqueue on macOS) — no polling.

Bridges from watchdog's background thread into the asyncio event loop via
call_soon_threadsafe so the ingestion callback runs as a proper coroutine.
One Observer per user; the manager can start/stop individual watchers at
runtime when a user updates their config.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Any
import logging
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class _PDFHandler(FileSystemEventHandler):
    """Forward new PDF files to an async callback via the event loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback,  # async def callback(user_id: str, path: str)
        user_id: str,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._callback = callback
        self._user_id = user_id

    def _dispatch_pdf(self, path: str) -> None:
        if path.lower().endswith(".pdf"):
            asyncio.run_coroutine_threadsafe(
                self._callback(self._user_id, path),
                self._loop,
            )

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._dispatch_pdf(str(event.src_path))

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._dispatch_pdf(str(event.dest_path))


class FolderWatcherManager:
    """
    Manages one watchdog Observer per user.

    Thread-safe stop/start because Observer.stop() + join() is called from
    the main asyncio thread. The Observer itself runs its own daemon thread.
    """

    def __init__(self) -> None:
        self._observers: Dict[str, Any] = {}

    def start_for_user(
        self,
        user_id: str,
        folder: str,
        callback,  # async def callback(user_id: str, path: str)
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """
        Start watching `folder` for new PDFs.  Stops any existing watcher
        for this user first.  Returns False if the folder does not exist.
        """
        self.stop_for_user(user_id)
        p = Path(folder)
        if not p.is_dir():
            logger.warning(
                "watch_start: folder does not exist user=%s path=%s", user_id, folder
            )
            return False
        handler = _PDFHandler(loop, callback, user_id)
        observer = Observer()
        observer.schedule(handler, str(p), recursive=False)
        observer.start()
        self._observers[user_id] = observer
        logger.info("watch_start: user=%s path=%s", user_id, folder)
        return True

    def stop_for_user(self, user_id: str) -> None:
        observer = self._observers.pop(user_id, None)
        if observer:
            observer.stop()
            # Run the join in a background thread to avoid blocking the main event loop
            try:
                loop = asyncio.get_event_loop()
                loop.run_in_executor(None, observer.join, 5)
            except Exception:
                # Fallback if no event loop is running
                observer.join(timeout=5)
            logger.info("watch_stop: user=%s", user_id)

    def stop_all(self) -> None:
        for uid in list(self._observers):
            self.stop_for_user(uid)

    def active_users(self) -> list[str]:
        return list(self._observers.keys())

    def is_active(self, user_id: str) -> bool:
        return user_id in self._observers
