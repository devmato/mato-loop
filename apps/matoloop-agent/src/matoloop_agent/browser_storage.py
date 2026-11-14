"""No-follow browser staging streams on the selected host."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

from .errors import AgentError


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
