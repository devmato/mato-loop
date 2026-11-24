"""Private browser tab checkpoints and no-follow staging streams."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import chain
from pathlib import Path
from time import time
from typing import BinaryIO, Literal
from uuid import UUID

from .browser_contracts import BrowserNavigationArguments
from .errors import AgentError

TAB_RETENTION_SECONDS = 7 * 24 * 60 * 60


def prune_tabs(root: Path) -> None:
    cutoff = time() - TAB_RETENTION_SECONDS
    for path in chain(root.glob("tabs-*.json"), root.glob(".tabs-*")):
        try:
            if path.lstat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def tab_checkpoint(root: Path, session_id: str) -> Path:
    return root / f"tabs-{UUID(session_id)}.json"


def restorable_url(url: str) -> str:
    try:
        if len(url) <= 4096:
            return BrowserNavigationArguments.manual_url(url) or "about:blank"
    except ValueError:
        pass
    return "about:blank"


def read_tabs(path: Path) -> tuple[list[str], int]:
    try:
        if path.lstat().st_mtime <= time() - TAB_RETENTION_SECONDS:
            return [], 0
        with browser_file(path) as handle:
            payload = handle.read(1_048_577)
        if len(payload) > 1_048_576:
            return [], 0
        data = json.loads(payload)
        urls, active = data["urls"], data["active"]
        if isinstance(urls, list) and 0 < len(urls) <= 32 and type(active) is int:
            if all(isinstance(url, str) and restorable_url(url) == url for url in urls):
                if any(url != "about:blank" for url in urls) and 0 <= active < len(urls):
                    return urls, active
    except (OSError, AgentError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return [], 0


def write_tabs(path: Path, urls: list[str], active: int) -> None:
    if not any(url != "about:blank" for url in urls):
        path.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tabs-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"urls": urls, "active": active}, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def browser_file(
    path: Path, mode: Literal["rb", "wb", "r+b"] = "rb", *, create: bool = False
) -> Iterator[BinaryIO]:
    if sys.platform == "win32":
        from . import windows_handles

        with windows_handles.parent(path.as_posix(), write=create) as (parent, name):
            try:
                with windows_handles.child(
                    parent, name, create=create, write=mode != "rb"
                ) as handle:
                    with windows_handles.stream(handle, mode) as stream:
                        yield stream
            except AgentError as exc:
                if create and exc.code == "path.exists":
                    raise FileExistsError(path) from exc
                raise
    else:
        flags = os.O_RDONLY if mode == "rb" else os.O_RDWR if "+" in mode else os.O_WRONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, mode) as stream:
            yield stream
