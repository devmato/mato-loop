from __future__ import annotations

import base64
import json
import math
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from .protocol import UUID_PATTERN, StrictModel

Id = Annotated[str, Field(pattern=UUID_PATTERN)]
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_OBSERVATION_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 65_536
MAX_CLIPBOARD_BYTES = 16 * 1024
BrowserErrorCode = Literal[
    "browser.unavailable",
    "browser.notReady",
    "browser.busy",
    "browser.notConnected",
    "browser.bindingChanged",
    "browser.setupFailed",
    "browser.dependenciesMissing",
    "browser.unsupported",
    "browser.sandboxUnavailable",
    "browser.actionFailed",
    "browser.invalidTarget",
    "browser.snapshotChanged",
    "browser.observationUnstable",
    "browser.fileTooLarge",
    "browser.profileOwnerMismatch",
    "browser.profileInUse",
    "browser.outcomeUnknown",
]
BrowserScriptFailureReason = Literal[
    "scriptTimeout",
    "playwrightTimeout",
    "targetUnavailable",
    "runtimeError",
    "invalidResult",
]
BrowserScriptFailureDetail = Literal[
    "resultTooLarge",
    "resultTooDeep",
    "resultTypeUnsupported",
    "typeError",
    "attributeError",
]


def decode_chunk(value: str) -> bytes:
    data = base64.b64decode(value, validate=True)
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError("Chunk exceeds 65536 bytes")
    return data


class BrowserStatusArguments(StrictModel):
    pass


class BrowserOwnerArguments(StrictModel):
    owner_id: Id = Field(alias="ownerId")


class BrowserIdArguments(StrictModel):
    browser_id: Id = Field(alias="browserId")


class BrowserOpenArguments(BrowserOwnerArguments, BrowserIdArguments):
    session_id: Id = Field(alias="sessionId")
    color_scheme: Literal["light", "dark"] = Field(default="light", alias="colorScheme")


class BrowserOpenViewArguments(BrowserOpenArguments):
    recover_only: bool = Field(default=False, alias="recoverOnly")
    defer_restore: bool = Field(default=False, alias="deferRestore")


class BrowserCloseArguments(BrowserIdArguments):
    preserve_tabs: bool = Field(default=False, alias="preserveTabs")


class BrowserViewArguments(BrowserIdArguments):
    relay_id: Id = Field(alias="relayId")
    relay_token: str = Field(alias="relayToken", min_length=16, max_length=8192)


class BrowserInput(StrictModel):
    kind: Literal["mouse", "key", "text", "release", "dialog"]
    tab_id: Id = Field(alias="tabId")
    input_epoch: Id = Field(alias="inputEpoch")
    event: Literal["move", "down", "up", "wheel"] | None = None
    x: float = Field(default=0, ge=0, le=3840, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=2160, allow_inf_nan=False)
    button: Literal["left", "middle", "right"] = "left"
    click_count: int = Field(default=1, alias="clickCount", ge=1, le=3)
    modifiers: int = Field(default=0, ge=0, le=15)
    delta_x: float = Field(default=0, alias="deltaX", ge=-10000, le=10000, allow_inf_nan=False)
    delta_y: float = Field(default=0, alias="deltaY", ge=-10000, le=10000, allow_inf_nan=False)
    key: str = Field(default="", max_length=100)
    code: str = Field(default="", max_length=100)
    key_code: int = Field(default=0, alias="keyCode", ge=0, le=255)
    text: str = Field(default="", max_length=MAX_CLIPBOARD_BYTES)
    accept: bool = False

    @model_validator(mode="after")
    def valid_event(self):
        if self.kind in {"mouse", "key"} and self.event is None:
            raise ValueError("Input event is required")
        if self.kind == "key" and (
            self.event not in {"down", "up"} or not self.key or not self.code
        ):
            raise ValueError("Invalid key event")
        if len(self.text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            raise ValueError("Input text exceeds 16384 bytes")
        return self


class BrowserControlArguments(BrowserIdArguments):
    manual: bool
    restore_tabs: bool = Field(default=False, alias="restoreTabs")
    origin: str | None = Field(default=None, min_length=1, max_length=255)
    control_epoch: int | None = Field(default=None, alias="controlEpoch", ge=0)

    @model_validator(mode="after")
    def complete_origin(self):
        if (self.origin is None) != (self.control_epoch is None):
            raise ValueError("Origin and controlEpoch must be supplied together")
        return self


class BrowserResizeResult(StrictModel):
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=240, le=2160)


class BrowserResizeArguments(BrowserIdArguments, BrowserResizeResult):
    pass


class BrowserClipboardResult(StrictModel):
    text: str = Field(max_length=MAX_CLIPBOARD_BYTES)

    @field_validator("text")
    @classmethod
    def bounded_utf8(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            raise ValueError("Clipboard exceeds 16384 UTF-8 bytes")
        return value


class BrowserClipboardArguments(BrowserIdArguments):
    action: Literal["write", "copy"]
    text: str | None = Field(default=None, max_length=MAX_CLIPBOARD_BYTES)

    @model_validator(mode="after")
    def valid_text(self):
        if self.action == "write":
            if self.text is None:
                raise ValueError("Clipboard write requires text")
            BrowserClipboardResult(text=self.text)
        elif self.text is not None:
            raise ValueError("Clipboard copy does not accept text")
        return self


class BrowserSetupIdArguments(StrictModel):
    setup_id: Id = Field(alias="setupId")


class BrowserSetupReadArguments(BrowserSetupIdArguments):
    offset: int = Field(ge=0)
    max_bytes: int = Field(default=MAX_CHUNK_BYTES, alias="maxBytes", ge=1, le=MAX_CHUNK_BYTES)
    wait_seconds: float = Field(default=0, alias="waitSeconds", ge=0, le=5)


class BrowserSetupWriteArguments(BrowserSetupIdArguments):
    data: str = Field(max_length=87_384)

    @field_validator("data")
    @classmethod
    def valid_chunk(cls, value: str) -> str:
        decode_chunk(value)
        return value


class BrowserSetupResizeArguments(BrowserSetupIdArguments):
    rows: int = Field(ge=1, le=1000)
    columns: int = Field(ge=1, le=1000)


class BrowserFormField(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    ref: str = Field(pattern=r"^(?:f[0-9]+)?e[0-9]+$")
    type: Literal["textbox", "checkbox", "radio", "combobox"]
    value: str = Field(
        max_length=32_768,
        description="Text for textbox; true/false for checkbox; true for radio; "
        "exact unique visible option label for a native select combobox.",
    )

    @model_validator(mode="after")
    def valid_value(self):
        if self.type in {"checkbox", "radio"} and self.value not in {"true", "false"}:
            raise ValueError("Checked values must be true or false")
        if self.type == "radio" and self.value != "true":
            raise ValueError("Radio fields cannot be unchecked")
        return self


class BrowserExecuteArguments(BrowserIdArguments):
    action: Literal[
        "tabs",
        "newtab",
        "closetab",
        "select",
        "navigate",
        "inspect",
        "screenshot",
        "click",
        "fill",
        "fill_form",
        "press",
        "scroll",
        "diagnostics",
        "upload",
        "run_code",
        "evaluate",
    ]
    tab_id: Id | None = Field(default=None, alias="tabId")
    url: str | None = Field(default=None, max_length=4096)
    selector: str | None = Field(default=None, max_length=2000)
    depth: int | None = Field(default=None, ge=1, le=100)
    text: str | None = Field(default=None, max_length=32_768)
    key: str | None = Field(default=None, max_length=100)
    direction: Literal["up", "down"] | None = None
    upload_id: Id | None = Field(default=None, alias="uploadId")
    observe: bool = False
    settle: bool = False
    guard: bool = False
    slowly: bool = False
    submit: bool = False
    code: str | None = Field(default=None, max_length=32_768)
    function: str | None = Field(default=None, max_length=32_768)
    fields: list[BrowserFormField] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("url")
    @classmethod
    def web_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Only HTTP(S) navigation is allowed")
        return value

    @model_validator(mode="after")
    def required_fields(self):
        required = {
            "navigate": (self.url,),
            "click": (self.selector,),
            "fill": (self.selector, self.text),
            "fill_form": (self.tab_id, self.fields),
            "press": (self.key,),
            "scroll": (self.direction,),
            "upload": (self.selector, self.upload_id),
            "select": (self.tab_id,),
            "run_code": (self.tab_id, self.code),
            "evaluate": (self.tab_id, self.function),
        }
        if any(value is None for value in required.get(self.action, ())):
            raise ValueError("Action requires additional fields")
        if (self.slowly or self.submit) and self.action != "fill":
            raise ValueError("Only fill accepts slowly and submit")
        if self.code is not None and self.action != "run_code":
            raise ValueError("Only run_code accepts code")
        if self.function is not None and self.action != "evaluate":
            raise ValueError("Only evaluate accepts function")
        if self.depth is not None and self.action != "inspect":
            raise ValueError("Only inspect accepts depth")
        if self.selector is not None and self.action not in {
            "inspect",
            "click",
            "fill",
            "press",
            "upload",
            "evaluate",
        }:
            raise ValueError("Action does not accept selector")
        if self.fields is not None:
            if self.action != "fill_form":
                raise ValueError("Only fill_form accepts fields")
            if len({field.ref for field in self.fields}) != len(self.fields):
                raise ValueError("Form refs must be unique")
            if sum(len(field.value) for field in self.fields) > 32_768:
                raise ValueError("Form values exceed 32768 characters")
        return self


class BrowserFileArguments(BrowserIdArguments):
    action: Literal["begin", "write", "finish", "read", "delete", "choose"]
    file_id: Id | None = Field(default=None, alias="fileId")
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    size: int | None = Field(default=None, ge=0, le=MAX_FILE_BYTES)
    offset: int = Field(default=0, ge=0, le=MAX_FILE_BYTES)
    data: str = Field(default="", max_length=87_384)
    max_bytes: int = Field(default=MAX_CHUNK_BYTES, alias="maxBytes", ge=1, le=MAX_CHUNK_BYTES)

    @field_validator("data")
    @classmethod
    def valid_chunk(cls, value: str) -> str:
        decode_chunk(value)
        return value

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str | None) -> str | None:
        if value is not None and (
            value in {".", ".."}
            or any(c in value for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Filename must be plain")
        return value

    @model_validator(mode="after")
    def required_fields(self):
        if self.action == "begin":
            if self.filename is None or self.size is None or self.file_id is not None:
                raise ValueError("Begin requires filename and size, not fileId")
        elif self.file_id is None:
            raise ValueError("Action requires fileId")
        return self


class BrowserFilesArguments(BrowserIdArguments):
    pass


class BrowserUploadArguments(BrowserIdArguments):
    path: str = Field(min_length=1, max_length=4096)


class BrowserNavigationArguments(BrowserIdArguments):
    action: Literal[
        "state", "select", "new", "close", "navigate", "back", "forward", "reload", "stop"
    ]
    tab_id: Id | None = Field(default=None, alias="tabId")
    url: str | None = Field(default=None, max_length=4096)

    @field_validator("url")
    @classmethod
    def manual_url(cls, value: str | None) -> str | None:
        if value == "about:blank":
            return value
        BrowserExecuteArguments.web_url(value)
        if value is not None:
            parsed = urlsplit(value)
            if "\\" in value or value != value.strip() or parsed.port == 0:
                raise ValueError("Invalid manual navigation URL")
        return value

    @model_validator(mode="after")
    def action_fields(self):
        fields = {
            "state": set(),
            "new": {"url"},
            "navigate": {"tab_id", "url"},
        }.get(self.action, {"tab_id"})
        supplied = self.model_fields_set - {"browser_id", "action"}
        if supplied - fields or (self.action != "new" and supplied != fields):
            raise ValueError("Invalid navigation action fields")
        if any(getattr(self, field) is None for field in supplied):
            raise ValueError("Navigation fields must not be null")
        return self


class BrowserStatus(StrictModel):
    state: Literal["missing", "installing", "ready", "error", "unsupported"]
    browser_version: str | None = Field(default=None, alias="browserVersion", max_length=100)
    playwright_version: str | None = Field(default=None, alias="playwrightVersion", max_length=100)
    missing: list[str] = Field(default_factory=list, max_length=20)
    setup_id: Id | None = Field(default=None, alias="setupId")
    error_code: BrowserErrorCode | None = Field(default=None, alias="errorCode")


class BrowserSetupReadResult(StrictModel):
    data: str = Field(max_length=87_384)
    offset: int = Field(ge=0)
    running: bool
    exit_code: int | None = Field(alias="exitCode")


class BrowserFileChunkResult(StrictModel):
    file_id: Id = Field(alias="fileId")
    filename: str | None = Field(default=None, max_length=255)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    offset: int = Field(ge=0, le=MAX_FILE_BYTES)
    complete: bool
    data: str | None = Field(default=None, max_length=87_384)


class BrowserFileOkResult(StrictModel):
    ok: Literal[True]


BrowserFileResult = BrowserFileChunkResult | BrowserFileOkResult
BROWSER_FILE_RESULT = TypeAdapter(BrowserFileResult)


class BrowserTab(StrictModel):
    id: Id
    url: str = Field(max_length=4096)
    title: str = Field(max_length=500)


class BrowserDownload(StrictModel):
    id: Id
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)

    _plain_filename = field_validator("filename")(BrowserFileArguments.plain_filename.__func__)


class BrowserDownloadStatus(StrictModel):
    id: Id
    filename: str = Field(min_length=1, max_length=255)
    status: Literal["pending", "ready", "failed"]
    origin: str | None = Field(default=None, min_length=1, max_length=255)
    control_epoch: int | None = Field(default=None, alias="controlEpoch", ge=0)
    size: int | None = Field(default=None, ge=0, le=MAX_FILE_BYTES)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: BrowserErrorCode | None = Field(default=None, alias="errorCode")

    _plain_filename = field_validator("filename")(BrowserFileArguments.plain_filename.__func__)

    @model_validator(mode="after")
    def valid_status(self):
        if (self.origin is None) != (self.control_epoch is None):
            raise ValueError("Origin and controlEpoch must agree")
        if self.status == "ready" and (self.size is None or self.sha256 is None):
            raise ValueError("Ready download requires size and sha256")
        if self.status != "ready" and self.sha256 is not None:
            raise ValueError("Only ready downloads have sha256")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("Failed download requires errorCode")
        return self


class BrowserFilesResult(StrictModel):
    downloads: list[BrowserDownloadStatus] = Field(max_length=32)


class BrowserNavigationTab(BrowserTab):
    loading: bool


class BrowserNavigationResult(StrictModel):
    tabs: list[BrowserNavigationTab] = Field(max_length=32)
    active_tab_id: Id | None = Field(alias="activeTabId")
    can_go_back: bool = Field(alias="canGoBack")
    can_go_forward: bool = Field(alias="canGoForward")
    loading: bool


class BrowserScreenshot(StrictModel):
    file_id: Id = Field(alias="fileId")
    size: int = Field(ge=0, le=MAX_OBSERVATION_BYTES)


class BrowserBatchResult(StrictModel):
    completed: int = Field(ge=0, le=20)
    total: int = Field(ge=1, le=20)
    error_code: BrowserErrorCode | None = Field(default=None, alias="errorCode")

    @model_validator(mode="after")
    def valid_prefix(self):
        if self.completed > self.total or (self.error_code is None) != (
            self.completed == self.total
        ):
            raise ValueError("Batch completion and error must agree")
        return self


class BrowserScriptResult(StrictModel):
    value: JsonValue = None
    output: str = Field(default="", max_length=32_768)
    output_truncated: bool = Field(default=False, alias="outputTruncated")
    error_code: BrowserErrorCode | None = Field(default=None, alias="errorCode")
    failure_reason: BrowserScriptFailureReason | None = Field(default=None, alias="failureReason")
    failure_detail: BrowserScriptFailureDetail | None = Field(default=None, alias="failureDetail")
    line: int | None = Field(default=None, ge=1, le=32_768)

    @model_validator(mode="after")
    def valid_failure(self):
        if self.failure_reason is not None and self.error_code is None:
            raise ValueError("Script failure reason requires an error outcome")
        if self.line is not None and self.failure_reason is None:
            raise ValueError("Script line requires a failure reason")
        if self.failure_detail is not None:
            expected = (
                "runtimeError"
                if self.failure_detail in {"typeError", "attributeError"}
                else "invalidResult"
            )
            if self.failure_reason != expected:
                raise ValueError("Script failure detail does not match its reason")
        return self

    @field_validator("value", mode="before")
    @classmethod
    def bounded_json(cls, value):
        # Check exact JSON types before Pydantic can coerce or describe arbitrary objects.
        remaining = 32_768

        def visit(item, depth):
            nonlocal remaining
            if depth > 32:
                raise ValueError("Script result exceeds depth limit")
            if remaining < 0:
                raise ValueError("Script result exceeds JSON limits")
            kind = type(item)
            if kind in (list, dict):
                remaining -= 2 + max(0, len(item) - 1)
                if kind is dict:
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ValueError("Script result requires string keys")
                        visit(key, depth + 1)
                        remaining -= 1
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            elif (
                item is None or kind in (str, bool, int) or (kind is float and math.isfinite(item))
            ):
                if kind is str and len(item) > 32_768:
                    raise ValueError("Script result exceeds JSON limits")
                remaining -= len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            else:
                raise ValueError("Script result must be finite JSON")
            if remaining < 0:
                raise ValueError("Script result exceeds JSON limits")

        visit(value, 0)
        return value


class BrowserCodeFeedback(StrictModel):
    """Equivalent Python patterns, not executed source or a guarantee of safe replay."""

    completed: str = Field(default="", max_length=32_768)
    attempted: str | None = Field(default=None, max_length=32_768)
    truncated: bool = False
    error_code: BrowserErrorCode | None = Field(default=None, alias="errorCode")

    @model_validator(mode="after")
    def bounded_code(self):
        if len(self.completed) + len(self.attempted or "") > 32_768:
            raise ValueError("Code feedback exceeds 32768 characters")
        if self.attempted is not None and self.error_code is None:
            raise ValueError("Attempted code requires an error outcome")
        return self


class BrowserActionResult(StrictModel):
    tabs: list[BrowserTab] = Field(default_factory=list, max_length=32)
    tab_id: Id | None = Field(default=None, alias="tabId")
    url: str | None = Field(default=None, max_length=4096)
    text: str | None = Field(default=None, max_length=32_768)
    screenshot: BrowserScreenshot | None = None
    snapshot: BrowserScreenshot | None = None
    downloads: list[BrowserDownload] = Field(default_factory=list, max_length=32)
    data: dict[str, str | bool | int] | None = None
    batch: BrowserBatchResult | None = None
    script: BrowserScriptResult | None = None
    playwright_code: BrowserCodeFeedback | None = Field(default=None, alias="playwrightCode")


BROWSER_ARGUMENT_MODELS = {
    "integration.browser.status": BrowserStatusArguments,
    "integration.browser.setup": BrowserOwnerArguments,
    "integration.browser.setupread": BrowserSetupReadArguments,
    "integration.browser.setupwrite": BrowserSetupWriteArguments,
    "integration.browser.setupresize": BrowserSetupResizeArguments,
    "integration.browser.open": BrowserOpenArguments,
    "integration.browser.openview": BrowserOpenViewArguments,
    "integration.browser.view": BrowserViewArguments,
    "integration.browser.close": BrowserCloseArguments,
    "integration.browser.execute": BrowserExecuteArguments,
    "integration.browser.file": BrowserFileArguments,
    "integration.browser.files": BrowserFilesArguments,
    "integration.browser.upload": BrowserUploadArguments,
    "integration.browser.control": BrowserControlArguments,
    "integration.browser.resize": BrowserResizeArguments,
    "integration.browser.clipboard": BrowserClipboardArguments,
    "integration.browser.navigation": BrowserNavigationArguments,
}
