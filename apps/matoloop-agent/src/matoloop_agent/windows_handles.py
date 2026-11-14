"""Minimal no-follow, handle-relative Windows file primitives."""

from __future__ import annotations

import ctypes
import msvcrt
import os
import stat
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from typing import BinaryIO
from uuid import uuid4

from .errors import AgentError
from .windows_paths import native


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum", wintypes.USHORT),
        ("buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security", ctypes.c_void_p),
        ("quality", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _RenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace", wintypes.BOOLEAN),
        ("root", wintypes.HANDLE),
        ("length", wintypes.ULONG),
        ("name", wintypes.WCHAR * 1),
    ]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_nt_create = _ntdll.NtCreateFile
_nt_create.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatusBlock),
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_nt_create.restype = ctypes.c_long
_nt_set = _ntdll.NtSetInformationFile
_nt_set.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_IoStatusBlock),
    ctypes.c_void_p,
    wintypes.ULONG,
    ctypes.c_int,
]
_nt_set.restype = ctypes.c_long
_close = _kernel.CloseHandle
_close.argtypes = [wintypes.HANDLE]
_close.restype = wintypes.BOOL
_create = _kernel.CreateFileW
_create.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_create.restype = wintypes.HANDLE
_attributes = _kernel.GetFileInformationByHandleEx
_attributes.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_attributes.restype = wintypes.BOOL
_set = _kernel.SetFileInformationByHandle
_set.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_set.restype = wintypes.BOOL

_READ_ATTR = 0x80
_LIST = 0x01
_TRAVERSE = 0x20
_DELETE = 0x10000
_SYNCHRONIZE = 0x100000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 7
_OPEN = 1
_CREATE = 2
_DIRECTORY = 1
_NON_DIRECTORY = 0x40
_SYNC = 0x20
_NOFOLLOW = 0x200000
_REPARSE_ATTRIBUTE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def _check_handle(handle: int, *, directory: bool | None = None) -> None:
    info = _AttributeTag()
    if not _attributes(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attributes & _REPARSE_ATTRIBUTE:
        raise AgentError("path.unsupportedType", "Reparse point cannot be traversed")
    if directory is not None and bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise AgentError("path.notDirectory" if directory else "path.notFile", "Invalid file type")


@contextmanager
def root(drive: str, *, write: bool = False) -> Iterator[int]:
    handle = _create(
        drive,
        _READ_ATTR | _LIST | _TRAVERSE | _SYNCHRONIZE | (0x6 if write else 0),
        _SHARE_ALL,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _check_handle(handle, directory=True)
        yield handle
    finally:
        _close(handle)


@contextmanager
def child(
    parent: int,
    name: str,
    *,
    directory: bool = False,
    create: bool = False,
    write: bool = False,
    delete: bool = False,
) -> Iterator[int]:
    value = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    unicode = _UnicodeString(length, length, ctypes.cast(value, ctypes.c_wchar_p))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(unicode), 0x40 | 0x1000, None, None
    )
    handle = wintypes.HANDLE()
    status = _IoStatusBlock()
    access = (
        _READ_ATTR
        | _SYNCHRONIZE
        | (_LIST | _TRAVERSE | (0x6 if write else 0) if directory else _GENERIC_READ)
        | (_GENERIC_WRITE if write and not directory else 0)
        | (_DELETE if delete else 0)
    )
    result = _nt_create(
        ctypes.byref(handle),
        access,
        ctypes.byref(attributes),
        ctypes.byref(status),
        None,
        0x10 if directory else 0x80,
        _SHARE_ALL,
        _CREATE if create else _OPEN,
        (_DIRECTORY if directory else _NON_DIRECTORY | _NOFOLLOW) | _SYNC,
        None,
        0,
    )
    if result < 0:
        if result & 0xFFFFFFFF in {0xC0000034, 0xC000003A}:
            raise AgentError("path.notFound", "Path does not exist")
        if result & 0xFFFFFFFF == 0xC0000035:
            raise AgentError("path.exists", "Path already exists")
        raise AgentError("path.unavailable", "Could not open file component")
    try:
        _check_handle(handle, directory=directory)
        yield handle.value
    finally:
        _close(handle)


@contextmanager
def directory(path: str, *, create: bool = False, write: bool = False) -> Iterator[int]:
    value = native(path)
    components = value[3:].split("\\") if len(value) > 3 else []
    from contextlib import ExitStack

    with ExitStack() as stack:
        handle = stack.enter_context(root(value[:3], write=write and not components))
        for index, part in enumerate(components):
            needs_write = write and index == len(components) - 1
            try:
                handle = stack.enter_context(child(handle, part, directory=True, write=needs_write))
            except AgentError as exc:
                if not create or exc.code != "path.notFound":
                    raise
                # An empty relative name reopens this handle; "." is invalid to NtCreateFile.
                with child(handle, "", directory=True, write=True) as writable_parent:
                    try:
                        handle = stack.enter_context(
                            child(
                                writable_parent,
                                part,
                                directory=True,
                                create=True,
                                write=needs_write,
                            )
                        )
                    except AgentError as create_error:
                        if create_error.code != "path.exists":
                            raise
                        handle = stack.enter_context(
                            child(handle, part, directory=True, write=needs_write)
                        )
        yield handle


@contextmanager
def parent(path: str, *, write: bool = False) -> Iterator[tuple[int, str]]:
    native(path)
    if len(path) == 3:
        raise AgentError("path.rootMutation", "Drive root cannot be modified")
    with directory(
        path.rsplit("/", 1)[0] if path.count("/") > 1 else path[:3], write=write
    ) as handle:
        yield handle, path.rsplit("/", 1)[-1]


def rename(handle: int, destination_parent: int, name: str, *, replace: bool = False) -> None:
    encoded = name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_RenameInformation) + len(encoded))
    information = _RenameInformation.from_buffer(buffer)
    information.replace = replace
    information.root = destination_parent
    information.length = len(encoded)
    name_offset = _RenameInformation.name.offset
    buffer[name_offset : name_offset + len(encoded)] = encoded
    # The NT API preserves RootDirectory for handle-relative destination names.
    status = _IoStatusBlock()
    result = _nt_set(handle, ctypes.byref(status), buffer, len(buffer), 10)
    if result < 0:
        if result & 0xFFFFFFFF == 0xC0000035:
            raise AgentError("path.exists", "Destination already exists")
        raise AgentError("path.unavailable", "Could not rename file")


def remove(handle: int) -> None:
    disposition = wintypes.BOOL(True)
    if not _set(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise AgentError("path.unavailable", "Could not remove file")


def temporary_name() -> str:
    return f".matoloop-{uuid4().hex}.tmp"


@contextmanager
def staged_file(parent: int, name: str, *, replace: bool) -> Iterator[int]:
    """Publish a completed temporary file, or remove it through its original handle."""
    with child(parent, temporary_name(), create=True, write=True, delete=True) as created:
        published = False
        try:
            yield created
            rename(created, parent, name, replace=replace)
            published = True
        finally:
            if not published:
                remove(created)


@contextmanager
def stream(handle: int, mode: str = "rb") -> Iterator[BinaryIO]:
    duplicate = wintypes.HANDLE()
    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = _kernel.GetCurrentProcess()
    duplicate_handle = _kernel.DuplicateHandle
    duplicate_handle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    if not duplicate_handle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        raise AgentError("path.unavailable", "Could not duplicate file handle")
    try:
        fd = msvcrt.open_osfhandle(
            duplicate.value,
            os.O_BINARY
            | (os.O_RDONLY if mode == "rb" else os.O_RDWR if "+" in mode else os.O_WRONLY),
        )
    except BaseException:
        _close(duplicate)
        raise
    with os.fdopen(fd, mode) as file:
        yield file


def names(handle: int) -> Iterator[tuple[str, int, int, float]]:
    """Enumerate metadata from an open directory, without reopening its name."""
    query = _ntdll.NtQueryDirectoryFile
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    query.restype = ctypes.c_long
    buffer = ctypes.create_string_buffer(65536)
    status = _IoStatusBlock()
    restart = True
    while True:
        result = query(
            handle,
            None,
            None,
            None,
            ctypes.byref(status),
            buffer,
            len(buffer),
            1,
            False,
            None,
            restart,
        )
        if result & 0xFFFFFFFF == 0x80000006:  # STATUS_NO_MORE_FILES
            return
        if result < 0:
            raise AgentError("path.unavailable", "Directory listing failed")
        position = 0
        while position + 64 <= status.information:
            next_entry = struct.unpack_from("<I", buffer, position)[0]
            attributes, name_length = struct.unpack_from("<II", buffer, position + 56)
            if name_length > 510 or position + 64 + name_length > status.information:
                raise AgentError("path.unavailable", "Directory listing is invalid")
            name = buffer[position + 64 : position + 64 + name_length].decode("utf-16-le")
            if name not in {".", ".."}:
                size = struct.unpack_from("<q", buffer, position + 40)[0]
                modified = struct.unpack_from("<q", buffer, position + 24)[0]
                yield name, attributes, max(0, size), (modified - 116444736000000000) / 10_000_000
            if not next_entry:
                break
            position += next_entry
        restart = False
