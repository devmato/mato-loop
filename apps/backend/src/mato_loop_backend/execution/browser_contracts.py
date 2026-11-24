from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Annotated, BinaryIO, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, PrivateAttr, field_validator, model_validator

from mato_loop_backend.execution.client import ExecutionResult
from mato_loop_backend.execution.protocol import WireModel

BrowserErrorCode = Literal[
    "browser.unavailable", "browser.notReady", "browser.busy", "browser.notConnected",
    "browser.bindingChanged", "browser.setupFailed", "browser.dependenciesMissing",
    "browser.unsupported", "browser.sandboxUnavailable", "browser.actionFailed",
    "browser.invalidTarget", "browser.fileTooLarge", "browser.profileOwnerMismatch",
    "browser.outcomeUnknown", "browser.profileInUse", "browser.snapshotChanged",
    "browser.observationUnstable",
]
BrowserScriptFailureReason = Literal[
    "scriptTimeout", "playwrightTimeout", "targetUnavailable", "runtimeError", "invalidResult",
]
BrowserScriptFailureDetail = Literal[
    "resultTooLarge", "resultTooDeep", "resultTypeUnsupported", "typeError", "attributeError",
]
BrowserAction = Literal[
    "tabs", "newtab", "closetab", "navigate", "inspect", "screenshot", "click",
    "fill", "fill_form", "press", "scroll", "diagnostics", "upload", "select", "evaluate", "run_code",
]
BROWSER_SNAPSHOT_ACTIONS = frozenset({
    "closetab", "inspect", "screenshot", "click", "fill", "fill_form", "press", "scroll", "upload",
    "navigate", "select", "evaluate", "run_code",
})
BrowserOpaqueId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
_ShortText = Annotated[str, Field(max_length=4096)]
MAX_BROWSER_FILE_BYTES = 1024 * 1024 * 1024
MAX_OBSERVATION_BYTES = 64 * 1024 * 1024
BROWSER_TRANSFER_TIMEOUT = 60 + MAX_BROWSER_FILE_BYTES / (256 * 1024)


class BrowserConfig(WireModel):
    pass


class BrowserResult(ExecutionResult):
    model_config = WireModel.model_config


class BrowserInspectionData(BrowserResult):
    model_config = ConfigDict(populate_by_name=False, validate_by_name=False)

    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    settled: bool = True
    snapshot_characters: int = Field(default=0, ge=0, le=MAX_BROWSER_FILE_BYTES)
    snapshot_scope: Literal["page", "element"] = "page"
    snapshot_depth: int | None = Field(default=None, ge=1, le=100)


class BrowserStatusResult(BrowserResult):
    state: Literal["missing", "installing", "ready", "error", "unsupported"]
    browser_version: str | None = Field(default=None, max_length=200)
    playwright_version: str | None = Field(default=None, max_length=200)
    missing: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=64)
    setup_id: UUID | None = Field(default=None, strict=False)
    error_code: BrowserErrorCode | None = None


class BrowserOwnerArguments(WireModel):
    owner_id: UUID = Field(strict=False)


class BrowserIdArguments(WireModel):
    browser_id: UUID = Field(strict=False)


class BrowserOpenResult(BrowserResult):
    upgrade_fields = frozenset({"protocol"})
    browser_id: UUID = Field(strict=False)
    protocol: Literal["chat-browser-v1"]
    restored_tabs: int = Field(default=0, ge=0, le=32)
    restore_incomplete: bool = False
    restore_pending: bool = False


class BrowserControlResult(BrowserResult):
    ok: bool


class BrowserRestoreResult(BrowserControlResult):
    restored_tabs: int = Field(default=0, ge=0, le=32)
    restore_incomplete: bool = False


class BrowserStreamResult(BrowserControlResult):
    upgrade_fields = frozenset({"protocol"})
    protocol: Literal["page-stream-v1"]


class BrowserResizeResult(BrowserResult):
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=240, le=2160)


class BrowserClipboard(BrowserResult):
    text: str = Field(max_length=16384, repr=False)

    @field_validator("text")
    @classmethod
    def bounded_utf8(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 16384:
            raise ValueError("clipboard exceeds limit")
        return value


def _setup_data(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid setup data") from exc
    if len(decoded) > 65536:
        raise ValueError("setup data exceeds limit")
    return value


class BrowserSetupReadResult(BrowserResult):
    data: str = Field(max_length=87384)
    offset: int = Field(ge=0)
    running: bool
    exit_code: int | None = None

    _validate_data = field_validator("data")(_setup_data)


class BrowserSetupWriteArguments(WireModel):
    setup_id: UUID = Field(strict=False)
    data: str = Field(max_length=87384, repr=False)

    _validate_data = field_validator("data")(_setup_data)


class BrowserSetupResizeArguments(WireModel):
    setup_id: UUID = Field(strict=False)
    rows: int = Field(ge=1, le=1000)
    columns: int = Field(ge=1, le=1000)


class BrowserFormField(WireModel):
    name: str = Field(min_length=1, max_length=200, description="Field name from the current inspection.")
    ref: str = Field(pattern=r"^(?:f[0-9]+)?e[0-9]+$", description="Inspection ref without the aria-ref= prefix.")
    type: Literal["textbox", "checkbox", "radio", "combobox"]
    value: str = Field(max_length=32768, description="Text for textbox; true/false for checkbox; true for radio; exact unique option label for a native select combobox.")

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if (self.type == "checkbox" and self.value not in {"true", "false"}) or (
            self.type == "radio" and self.value != "true"
        ):
            raise ValueError("invalid browser form field value")
        return self


class BrowserActionArguments(WireModel):
    action: BrowserAction = Field(description="Browser action. Inspect to discover targets; inspect again when page changes may affect them.")
    tab_id: BrowserOpaqueId | None = Field(default=None, description="Tab id returned by tabs or newtab; required for page actions.")
    url: str | None = Field(default=None, min_length=1, max_length=4096, description="HTTP(S) URL for navigate or newtab only. No credentials or local files.")
    selector: str | None = Field(default=None, min_length=1, max_length=2000, description="Playwright selector from page inspection; required for click, fill, and upload. Optional for inspect, press, and evaluate.")
    depth: int | None = Field(default=None, ge=1, le=100, description="Maximum accessibility tree depth for inspect only.")
    text: str | None = Field(default=None, max_length=32768, description="Literal text for fill only; may be empty to clear the input.")
    code: str | None = Field(default=None, max_length=32768)
    function: str | None = Field(default=None, max_length=32768)
    slowly: bool = False
    submit: bool = False
    fields: list[BrowserFormField] | None = Field(default=None, min_length=1, max_length=20, description="Ordered independent native fields for fill_form only. Unique refs from one snapshot; at most 32768 value characters in total. The whole list is authorized once, not each field separately.")
    key: str | None = Field(default=None, min_length=1, max_length=100, description="Playwright key or key chord for press only, such as Enter or Control+A.")
    direction: Literal["up", "down"] | None = Field(default=None, description="Scroll direction; required for scroll only.")
    upload_id: BrowserOpaqueId | None = Field(default=None, description="Opaque file staging id supplied by the human browser UI; required for upload. Never a filesystem path.")
    observe: bool = Field(default=True, description="Return the resulting accessibility snapshot after a write action. Set false when browser reading is unavailable. Ignored for tabs, inspect, screenshot, and diagnostics.")

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        required = {
            "navigate": {"tab_id", "url"}, "closetab": {"tab_id"},
            "inspect": {"tab_id"}, "screenshot": {"tab_id"},
            "click": {"tab_id", "selector"}, "fill": {"tab_id", "selector", "text"},
            "fill_form": {"tab_id", "fields"},
            "press": {"tab_id", "key"}, "scroll": {"tab_id", "direction"},
            "upload": {"tab_id", "selector", "upload_id"},
            "select": {"tab_id"}, "evaluate": {"tab_id", "function"},
            "run_code": {"tab_id", "code"},
        }.get(self.action, set())
        allowed = required | (
            {"url"} if self.action == "newtab"
            else {"selector", "depth"} if self.action == "inspect"
            else {"selector"} if self.action in {"press", "evaluate"}
            else set()
        )
        supplied = {
            name for name in BrowserActionArguments.model_fields
            if name not in {"action", "observe", "slowly", "submit"} and getattr(self, name) is not None
        }
        if not required <= supplied or not supplied <= allowed:
            raise ValueError("fields do not match browser action")
        if self.action != "fill" and (self.slowly or self.submit):
            raise ValueError("typing options require fill")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "\\" in self.url or any(ord(c) <= 32 or ord(c) == 127 for c in self.url)
            ):
                raise ValueError("browser navigation requires an HTTP(S) URL without credentials")
            _ = parsed.port
        has_ref = self.selector is not None and "aria-ref=" in self.selector
        uses_ref = has_ref and re.fullmatch(r"aria-ref=(?:f[0-9]+)?e[0-9]+", self.selector) is not None
        if has_ref and not uses_ref:
            raise ValueError("aria-ref selectors must contain exactly one inspection ref")
        if self.fields is not None and (
            len({field.ref for field in self.fields}) != len(self.fields)
            or sum(len(field.value) for field in self.fields) > 32768
        ):
            raise ValueError("browser form fields require unique refs and bounded total values")
        return self


class BrowserExecuteArguments(BrowserActionArguments, BrowserIdArguments):
    settle: bool = False
    guard: bool = False


class BrowserTab(BrowserResult):
    id: BrowserOpaqueId
    url: _ShortText
    title: str = Field(max_length=500)


class BrowserNavigationArguments(WireModel):
    action: Literal["state", "select", "new", "close", "navigate", "back", "forward", "reload", "stop"]
    tab_id: BrowserOpaqueId | None = None
    url: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        fields = {"state": set(), "new": {"url"},
                  "navigate": {"tab_id", "url"}}.get(self.action, {"tab_id"})
        supplied = self.model_fields_set & {"tab_id", "url"}
        if supplied - fields or (self.action != "new" and supplied != fields):
            raise ValueError("fields do not match navigation action")
        if any(getattr(self, name) is None for name in supplied):
            raise ValueError("navigation fields cannot be null")
        if self.url is not None and self.url != "about:blank":
            BrowserActionArguments(action="newtab", url=self.url)
            if urlsplit(self.url).port == 0:
                raise ValueError("invalid navigation port")
        return self


class BrowserNavigationTab(BrowserTab):
    loading: bool


class BrowserNavigationResult(BrowserResult):
    tabs: list[BrowserNavigationTab] = Field(max_length=32)
    active_tab_id: BrowserOpaqueId | None
    can_go_back: bool
    can_go_forward: bool
    loading: bool

    @model_validator(mode="after")
    def valid_active_tab(self) -> Self:
        ids = [tab.id for tab in self.tabs]
        if len(ids) != len(set(ids)) or (self.active_tab_id is not None and self.active_tab_id not in ids):
            raise ValueError("inconsistent browser tabs")
        return self


class BrowserDownload(BrowserResult):
    id: BrowserOpaqueId
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=MAX_BROWSER_FILE_BYTES)

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        if value in {".", ".."} or any(c in value for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid download filename")
        return value


class BrowserOrigin(WireModel):
    id: UUID = Field(strict=False)
    kind: Literal["human", "model"]
    owner_id: UUID = Field(strict=False)
    session_id: UUID = Field(strict=False)
    integration_id: UUID = Field(strict=False)
    run_id: UUID | None = Field(default=None, strict=False)
    view_id: UUID | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def valid_kind(self) -> Self:
        if (self.kind == "model") != (self.run_id is not None) or (
            self.kind == "human"
        ) != (self.view_id is not None):
            raise ValueError("browser origin fields do not match kind")
        return self


class BrowserDownloadState(BrowserResult):
    id: BrowserOpaqueId
    filename: str = Field(min_length=1, max_length=255)
    status: Literal["pending", "ready", "failed"]
    origin: str | None = Field(default=None, min_length=1, max_length=255)
    control_epoch: int | None = Field(default=None, ge=0)
    size: int | None = Field(default=None, ge=0, le=MAX_BROWSER_FILE_BYTES)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: BrowserErrorCode | None = None

    @model_validator(mode="after")
    def valid_status(self) -> Self:
        BrowserDownload.plain_filename(self.filename)
        if (self.origin is None) != (self.control_epoch is None):
            raise ValueError("origin and control epoch must agree")
        if self.status == "ready" and (self.size is None or self.sha256 is None):
            raise ValueError("ready download requires size and sha256")
        if self.status != "ready" and self.sha256 is not None:
            raise ValueError("only ready downloads have sha256")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("failed download requires error code")
        return self


class BrowserFileListResult(BrowserResult):
    downloads: list[BrowserDownloadState] = Field(default_factory=list, max_length=32)


class BrowserPublishedDownloadResult(BrowserResult):
    root: Literal["files"]
    file_id: UUID = Field(strict=False)
    revision: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=4096)
    size: int = Field(ge=0, le=MAX_BROWSER_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrowserScreenshot(BrowserResult):
    file_id: BrowserOpaqueId
    size: int = Field(ge=0, le=MAX_OBSERVATION_BYTES)


class BrowserSnapshot(BrowserScreenshot):
    pass


class BrowserSnapshotFile(BrowserResult):
    root: Literal["files"] = "files"
    path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_OBSERVATION_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrowserBatchResult(BrowserResult):
    completed: int = Field(ge=0, le=20, description="Number of successfully completed fields at the start of the ordered list. Never replay this prefix.")
    total: int = Field(ge=1, le=20)
    error_code: BrowserErrorCode | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if self.completed > self.total or (self.error_code is None) != (self.completed == self.total):
            raise ValueError("inconsistent browser batch progress")
        return self


class BrowserScriptResult(BrowserResult):
    value: JsonValue = None
    output: str = Field(default="", max_length=32768)
    output_truncated: bool = False
    error_code: BrowserErrorCode | None = None
    failure_reason: BrowserScriptFailureReason | None = None
    failure_detail: BrowserScriptFailureDetail | None = None
    line: int | None = Field(default=None, ge=1, le=32768)

    @model_validator(mode="after")
    def bounded_script(self) -> Self:
        if len(json.dumps(self.value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > 32768:
            raise ValueError("browser script value exceeds limit")
        if self.failure_reason is not None and self.error_code is None:
            raise ValueError("browser script failure reason requires an error outcome")
        if self.line is not None and self.failure_reason is None:
            raise ValueError("browser script line requires a failure reason")
        if self.failure_detail is not None:
            expected = "runtimeError" if self.failure_detail in {"typeError", "attributeError"} else "invalidResult"
            if self.failure_reason != expected:
                raise ValueError("browser script failure detail does not match its reason")
        return self


class BrowserCodeFeedback(BrowserResult):
    completed: str = Field(default="", max_length=32768)
    attempted: str | None = Field(default=None, max_length=32768)
    truncated: bool = False
    error_code: BrowserErrorCode | None = None

    @model_validator(mode="after")
    def bounded_code(self) -> Self:
        if len(self.completed) + len(self.attempted or "") > 32768:
            raise ValueError("browser code feedback exceeds limit")
        if self.attempted is not None and self.error_code is None:
            raise ValueError("attempted browser code requires an error outcome")
        return self


class BrowserActionResult(BrowserResult):
    tabs: list[BrowserTab] = Field(default_factory=list, max_length=32)
    tab_id: BrowserOpaqueId | None = None
    url: _ShortText | None = None
    text: str | None = Field(default=None, max_length=32768)
    screenshot: BrowserScreenshot | None = None
    _screenshot_bytes: bytes | None = PrivateAttr(default=None)
    snapshot: BrowserSnapshot | None = None
    snapshot_file: BrowserSnapshotFile | None = None
    _snapshot_stream: BinaryIO | None = PrivateAttr(default=None)
    downloads: list[BrowserDownload] = Field(default_factory=list, max_length=32)
    data: dict[str, JsonValue] | None = Field(default=None, max_length=32)
    batch: BrowserBatchResult | None = None
    script: BrowserScriptResult | None = None
    playwright_code: BrowserCodeFeedback | None = None

    def matches_action(self, arguments: BrowserActionArguments) -> bool:
        if self.playwright_code is not None and arguments.action not in {
            "newtab", "closetab", "navigate", "select", "click", "fill", "fill_form", "press", "scroll",
        }:
            return False
        if (self.script is not None) != (arguments.action in {"evaluate", "run_code"}):
            return False
        if arguments.action == "fill_form":
            return self.batch is not None and self.batch.total == len(arguments.fields)
        return self.batch is None

    @model_validator(mode="after")
    def bounded_result(self) -> Self:
        if self.data is not None and len(json.dumps(self.data, allow_nan=False).encode()) > 32768:
            raise ValueError("browser diagnostics exceed limit")
        if len(self.model_dump_json().encode()) > 900000:
            raise ValueError("browser result exceeds limit")
        return self


class BrowserFileArguments(BrowserIdArguments):
    action: Literal["begin", "write", "finish", "read", "delete", "choose"]
    file_id: BrowserOpaqueId | None = None
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    size: int | None = Field(default=None, ge=0, le=MAX_BROWSER_FILE_BYTES)
    offset: int = Field(default=0, ge=0, le=MAX_BROWSER_FILE_BYTES)
    data: str = Field(default="", max_length=87384, repr=False)
    max_bytes: int = Field(default=65536, ge=1, le=65536)

    _validate_data = field_validator("data")(_setup_data)

    @model_validator(mode="after")
    def validate_file(self) -> Self:
        if self.filename is not None:
            BrowserDownload.plain_filename(self.filename)
        if self.action == "begin":
            if self.filename is None or self.size is None or self.file_id is not None:
                raise ValueError("begin requires filename and size only")
        elif self.file_id is None:
            raise ValueError("file action requires fileId")
        return self


class BrowserFileResult(BrowserResult):
    file_id: BrowserOpaqueId
    offset: int = Field(ge=0, le=MAX_BROWSER_FILE_BYTES)
    size: int = Field(ge=0, le=MAX_BROWSER_FILE_BYTES)
    complete: bool
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    data: str | None = Field(default=None, max_length=87384)

    @model_validator(mode="after")
    def validate_file(self) -> Self:
        if self.filename is not None:
            BrowserDownload.plain_filename(self.filename)
        if self.data is not None:
            _setup_data(self.data)
        if self.offset > self.size or (self.complete and self.offset != self.size):
            raise ValueError("inconsistent browser file progress")
        return self
