from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from mato_loop_backend.automations import next_run_at
from mato_loop_backend.models import MemoryCategory
from mato_loop_backend.settings.models import TrustLevel, normalize_agent_display_name

QR_CODE_MAX_CONTENT_BYTES = 2_000


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtectedTextRef(ToolArguments):
    value_ref: UUID | Annotated[
        str,
        Field(pattern=r"^(?:run|session)-secret:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
    ]


class ProtectedText(ToolArguments):
    parts: list[str | ProtectedTextRef] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def needs_a_reference(self) -> ProtectedText:
        if not any(isinstance(part, ProtectedTextRef) for part in self.parts):
            raise ValueError("protected text needs a value reference")
        return self


class DelegatedTask(ToolArguments):
    description: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=8_000)

    @field_validator("description", "prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("prompt must not be blank")
        return cleaned


class TaskArguments(ToolArguments):
    tasks: list[DelegatedTask] = Field(min_length=1, max_length=4)


class NotifyArguments(ToolArguments):
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=500)

    @field_validator("title", "body")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        if not (value := " ".join(value.split())):
            raise ValueError("notification text must not be blank")
        return value


class TodoItem(ToolArguments):
    content: str = Field(min_length=1, max_length=160)
    status: Literal["pending", "in_progress", "completed", "cancelled"]

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("todo content must not be blank")
        return cleaned


class TodoWriteArguments(ToolArguments):
    todos: list[TodoItem] = Field(max_length=12)

    @model_validator(mode="after")
    def only_one_item_may_be_in_progress(self) -> TodoWriteArguments:
        if sum(item.status == "in_progress" for item in self.todos) > 1:
            raise ValueError("only one todo may be in progress")
        return self


class QuestionOption(ToolArguments):
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("label")
    @classmethod
    def label_must_not_be_blank(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("label must not be blank")
        return cleaned


class QuestionBase(ToolArguments):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
    header: str = Field(min_length=1, max_length=30)
    question: str = Field(min_length=1, max_length=2_000)
    default_answers: list[str] = Field(
        default_factory=list,
        max_length=13,
        description="Answers to use when the user submits no answer.",
    )

    @field_validator("header", "question")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("question text must not be blank")
        return cleaned


class QuestionItem(QuestionBase):
    type: Literal["free_text", "single_choice", "multiple_choice"] = Field(
        description=(
            "free_text: text; single_choice: exactly one option; "
            "multiple_choice: one or more options."
        )
    )
    options: list[QuestionOption] = Field(
        default_factory=list,
        max_length=12,
        description="Empty for free_text; 2-12 items for choices.",
    )
    allow_custom: bool = Field(
        default=True,
        description="Allow one custom text answer; do not add an Other option.",
    )
    placeholder: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "free_text only. Omit or use null for choices, even with allow_custom=true."
        ),
    )

    @model_validator(mode="after")
    def shape_matches_type(self) -> QuestionItem:
        if self.type == "free_text":
            if self.options:
                raise ValueError("free-text question must not have options")
            self.allow_custom = False
            return self
        if len(self.options) < 2:
            raise ValueError("choice question requires at least two options")
        if self.placeholder is not None:
            raise PydanticCustomError(
                "question_placeholder_not_allowed",
                "choice question must not have a placeholder",
            )
        labels = [option.label for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("option labels must be unique")
        return self


class QuestionArguments(ToolArguments):
    questions: list[QuestionItem] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def ids_and_defaults_are_valid(self) -> QuestionArguments:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be unique")
        for question in self.questions:
            _validate_question_answer(question, question.default_answers)
        return self


class PersonalVariableSaveProposal(ToolArguments):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
    description: str | None = Field(default=None, max_length=500)
    secret: bool
    action: Literal["create", "update"]
    variable_id: UUID | None = None
    agent_profile_id: UUID | None = None
    trust_level: TrustLevel

    @model_validator(mode="after")
    def target_matches_action(self) -> PersonalVariableSaveProposal:
        if (self.action == "update") != (self.variable_id is not None):
            raise ValueError("update requires variable_id")
        return self


class AdministratorQuestionTarget(ToolArguments):
    agent_id: UUID
    alias: str


class ProtectedQuestionItem(QuestionBase):
    type: Literal["value", "password"]
    save_proposal: PersonalVariableSaveProposal | None = None
    one_time: bool = False
    administrator: AdministratorQuestionTarget | None = None

    @model_validator(mode="after")
    def type_matches_proposal(self) -> ProtectedQuestionItem:
        if self.default_answers:
            raise ValueError("protected questions cannot have defaults")
        if self.administrator is not None and (not self.one_time or self.type != "password"):
            raise ValueError("administrator input requires a temporary password")
        if (self.save_proposal is None) == (not self.one_time):
            raise ValueError("protected question requires exactly one purpose")
        if self.save_proposal is not None and (
            (self.type == "password") != self.save_proposal.secret
        ):
            raise ValueError("question type must match secret setting")
        return self


class ProtectedQuestionArguments(ToolArguments):
    questions: list[ProtectedQuestionItem] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def purposes_and_ids_are_valid(self) -> ProtectedQuestionArguments:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be unique")
        if any(question.one_time for question in self.questions) and not all(
            question.one_time for question in self.questions
        ):
            raise ValueError("protected question purposes cannot be mixed")
        if not self.questions[0].one_time and len(self.questions) != 1:
            raise ValueError("saved protected questions require exactly one item")
        return self


QuestionRequestArguments = QuestionArguments | ProtectedQuestionArguments


@dataclass(frozen=True, slots=True)
class ProtectedQuestionResolution:
    answers: dict[str, list[str]]
    remember_for_session: frozenset[str] = frozenset()


def validate_protected_question_answers(
    questions: ProtectedQuestionArguments, answers: dict[str, list[str]]
) -> dict[str, list[str]]:
    if set(answers) != {question.id for question in questions.questions}:
        raise ValueError("protected question requires one answer per item")
    validated = {}
    for question in questions.questions:
        answer = answers.get(question.id)
        if not isinstance(answer, list) or len(answer) > 1:
            raise ValueError("protected question allows at most one answer per item")
        if not answer:
            validated[question.id] = []
            continue
        value = answer[0]
        if (
            not isinstance(value, str)
            or not value
            or "\0" in value
            or len(value) > 20_000
        ):
            raise ValueError("protected answer is invalid")
        if question.administrator is not None and (len(value) > 4096 or any(char in value for char in "\r\n")):
            raise ValueError("administrator password must be a single line of at most 4096 characters")
        validated[question.id] = [value]
    return validated


def _validate_question_answer(question: QuestionItem, answer: list[str]) -> list[str]:
    if not isinstance(answer, list) or len(answer) != len(set(answer)):
        raise ValueError("answer must be a list of unique strings")
    cleaned = []
    for item in answer:
        if not isinstance(item, str) or len(item) > 20_000:
            raise ValueError("answer item is invalid")
        value = item.strip()
        if value:
            cleaned.append(value)
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("answer items must be unique")
    if question.type == "free_text":
        if len(cleaned) > 1:
            raise ValueError("free-text answer is invalid")
    elif question.type == "single_choice":
        options = {option.label for option in question.options}
        if len(cleaned) > 1 or (
            cleaned and cleaned[0] not in options and not question.allow_custom
        ):
            raise ValueError("single-choice answer is invalid")
    else:
        options = {option.label for option in question.options}
        custom = [item for item in cleaned if item not in options]
        if custom and (not question.allow_custom or len(custom) > 1):
            raise ValueError("multiple-choice answer is invalid")
    return cleaned


def validate_question_answers(
    questions: QuestionArguments, answers: dict[str, list[str]]
) -> dict[str, list[str]]:
    if set(answers) != {question.id for question in questions.questions}:
        raise ValueError("every question requires exactly one answer")
    validated: dict[str, list[str]] = {}
    for question in questions.questions:
        answer = _validate_question_answer(question, answers[question.id])
        validated[question.id] = answer or _validate_question_answer(
            question, question.default_answers
        )
    return validated


class SessionFileReadArguments(ToolArguments):
    root: Literal["session", "workspace"]
    session_id: UUID | None = None

    @model_validator(mode="after")
    def session_target_requires_session_root(self) -> SessionFileReadArguments:
        if self.session_id is not None and self.root != "session":
            raise ValueError("session_id requires root=session")
        return self


class ElevatedArguments(ToolArguments):
    elevated: bool = False
    sudo_password_ref: UUID | Annotated[
        str,
        Field(pattern=r"^(?:run|session)-secret:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
    ] | None = None

    @model_validator(mode="after")
    def valid_elevation(self):
        if self.sudo_password_ref is not None and not self.elevated:
            raise ValueError("Sudo reference requires elevated execution")
        if self.elevated and getattr(self, "root", "workspace") != "workspace":
            raise ValueError("Elevated access requires the computer root")
        return self


class ReadArguments(SessionFileReadArguments, ElevatedArguments):
    path: str = Field(min_length=1, max_length=1_000)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=1_000, ge=1, le=1_000)
    pdf_pages: list[Annotated[int, Field(ge=1, strict=True)]] = Field(
        default_factory=list, max_length=8,
        description="PDF visual pages (1-based).",
    )

    @model_validator(mode="after")
    def pdf_requires_regular_read(self) -> ReadArguments:
        if self.pdf_pages and (self.elevated or not self.path.lower().endswith(".pdf")):
            raise ValueError("PDF pages require a non-elevated PDF read")
        self.pdf_pages = sorted(set(self.pdf_pages))
        return self


class GlobArguments(SessionFileReadArguments):
    pattern: str = Field(min_length=1, max_length=1_000, pattern=r"^[^\x00]+$")
    path: str = Field(default=".", max_length=1_000)


class GrepArguments(GlobArguments):
    pattern: str = Field(min_length=1, max_length=4_096, pattern=r"^[^\x00]+$")
    include: str | None = Field(
        default=None, min_length=1, max_length=1_000, pattern=r"^[^\x00]+$"
    )


class ProvideFileArguments(SessionFileReadArguments):
    path: str | None = Field(default=None, min_length=1, max_length=1_000)
    paths: list[Annotated[str, Field(min_length=1, max_length=1_000)]] | None = Field(
        default=None, min_length=1, max_length=1_000
    )

    @model_validator(mode="after")
    def select_source(self) -> ProvideFileArguments:
        if (self.path is None) == (self.paths is None):
            raise ValueError("provide exactly one of path or paths")
        return self


class WriteFileArguments(ElevatedArguments):
    root: Literal["session", "workspace"]
    path: str = Field(min_length=1, max_length=1_000)
    content: Annotated[str, Field(max_length=1_000_000)] | ProtectedText
    expected_sha256: str | None = Field(
        default=None, alias="expectedSha256", pattern=r"^[0-9a-f]{64}$"
    )


class EditFileArguments(ElevatedArguments):
    root: Literal["session", "workspace"]
    path: str = Field(min_length=1, max_length=1_000)
    old_text: Annotated[str, Field(min_length=1, max_length=1_000_000)] | ProtectedText = Field(alias="oldText")
    new_text: Annotated[str, Field(max_length=1_000_000)] | ProtectedText = Field(alias="newText")
    replace_all: bool = Field(default=False, alias="replaceAll")
    expected_sha256: str | None = Field(
        default=None, alias="expectedSha256", pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def require_change(self) -> EditFileArguments:
        if self.old_text == self.new_text:
            raise ValueError("old_text and new_text must differ")
        return self


class CreateDirectoryArguments(ToolArguments):
    root: Literal["session", "workspace"]
    path: str = Field(min_length=1, max_length=1_000)


class ApplyPatchArguments(ToolArguments):
    patch: str = Field(min_length=1, max_length=512 * 1024)
    expected_sha256: dict[
        Annotated[str, Field(min_length=1, max_length=4_096)],
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ] = Field(default_factory=dict, alias="expectedSha256", max_length=1_000)

    @field_validator("patch")
    @classmethod
    def validate_patch_envelope(cls, value: str) -> str:
        if (
            "\r" in value
            or "\0" in value
            or not value.startswith("*** Begin Patch\n")
            or value.rstrip("\n").split("\n")[-1] != "*** End Patch"
        ):
            raise ValueError("patch must use the apply_patch envelope and LF text")
        return value


class DeletePathArguments(ToolArguments):
    root: Literal["session", "workspace"]
    path: str = Field(min_length=1, max_length=1_000)
    recursive: bool = False


class CopyPathArguments(ToolArguments):
    source_root: Literal["session", "workspace"]
    source_path: str = Field(min_length=1, max_length=1_000)
    destination_root: Literal["session", "workspace"]
    destination_path: str = Field(min_length=1, max_length=1_000)


class MovePathArguments(CopyPathArguments):
    pass


class ShellRunArguments(ElevatedArguments):
    command: str = Field(min_length=1, max_length=256 * 1024)
    environment: dict[
        Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")],
        UUID
        | Annotated[
            str,
            Field(
                pattern=r"^(?:run|session)-secret:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            ),
        ],
    ] = Field(default_factory=dict, max_length=32)
    timeout: float | None = Field(
        default=None, strict=True, gt=0, le=3600,
        description="Time limit in seconds. Null uses the agent default; the local agent maximum applies.",
    )

    @field_validator("environment")
    @classmethod
    def environment_must_not_replace_host_identity(
        cls, value: dict[str, UUID | str]
    ) -> dict[str, UUID | str]:
        protected = {
            "BASHOPTS",
            "BASH_ENV",
            "CDPATH",
            "CLASSPATH",
            "ENV",
            "GLOBIGNORE",
            "HOME",
            "IFS",
            "JAVA_TOOL_OPTIONS",
            "LANG",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "LOGNAME",
            "NODE_OPTIONS",
            "NODE_PATH",
            "PATH",
            "PERL5LIB",
            "PERL5OPT",
            "PS4",
            "PWD",
            "PYTHONHOME",
            "PYTHONPATH",
            "RUBYLIB",
            "RUBYOPT",
            "SHELL",
            "SHELLOPTS",
            "USER",
            "ZDOTDIR",
            "_JAVA_OPTIONS",
        }
        if protected.intersection(value) or any(
            name.startswith(("DYLD_", "GIT_CONFIG_")) for name in value
        ):
            raise ValueError("environment contains a protected name")
        return value

class VariableReadArguments(ToolArguments):
    name: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,119}$"
    )


class VariableWriteArguments(ToolArguments):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Optional purpose; never include the value.",
    )
    secret: bool
    variable_id: UUID | None = None


class SecretInputField(ToolArguments):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    label: str = Field(min_length=1, max_length=30)
    question: str = Field(min_length=1, max_length=2_000)
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Optional purpose for session reuse; never include the value.",
    )
    masked: bool = True
    refresh: bool = Field(
        default=False,
        description="Request a new value for this name instead of reusing a stored value. Use after rejected authentication.",
    )
    administrator: bool = Field(
        default=False,
        description="Authenticate to the selected computer with this password input; actions require separate authorization.",
    )

    @field_validator("label", "question")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("input text must not be blank")
        return cleaned


class SecretInputArguments(ToolArguments):
    inputs: list[SecretInputField] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def names_are_unique(self) -> SecretInputArguments:
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input names must be unique")
        if sum(item.administrator for item in self.inputs) > 1 or any(
            item.administrator and not item.masked for item in self.inputs
        ):
            raise ValueError("administrator input requires one masked field")
        return self


class SystemInfoArguments(ToolArguments):
    pass


class ChangeDirectoryArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4096)


class ListExecutionAgentsArguments(ToolArguments):
    pass


class SelectExecutionAgentArguments(ToolArguments):
    agent_id: UUID


class WebSearchArguments(ToolArguments):
    query: str = Field(
        min_length=1,
        max_length=2_000,
        description=(
            "One compact search-engine query for one information need. Preserve user-supplied dates, "
            "versions, model numbers, and exact names; never add a date."
        ),
    )
    search_type: Literal["web", "images"] = "web"


class WebFetchArguments(ToolArguments):
    url: str = Field(min_length=1, max_length=1_200)
    format: Literal["markdown", "text", "html"] = "markdown"
    timeout: int | None = Field(
        default=None, ge=1, le=120,
        description="Total timeout in seconds. Omit for the configured provider default.",
    )
    pdf_pages: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "PDF pages to attach as images for layout; omit for text."
        ),
    )

    @field_validator("pdf_pages")
    @classmethod
    def normalize_pdf_pages(cls, value: list[int]) -> list[int]:
        return sorted(set(value))


class JinaWebFetchArguments(WebFetchArguments):
    engine: Literal["auto", "browser", "curl"] = Field(
        default="auto", description="Browser renders JavaScript, not automatic consent/login. Stored cookies require browser; auto selects it, curl is rejected for those targets.",
    )
    remove_overlay: bool | None = Field(default=None, description="Remove overlays; omit for the connection default.")
    no_cache: bool = Field(default=False, description="Bypass Reader's existing cache for fresh content.")
    target_selector: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$", description="CSS selector for the content to extract; also waits for it.")
    wait_for_selector: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$", description="CSS selector to wait for before extraction.")
    remove_selector: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$", description="CSS selector for elements to exclude.")
    with_links_summary: bool = Field(default=False, description="Include the page's link overview.")
    with_images_summary: bool = Field(default=False, description="Include image URLs, not image attachments or generated descriptions.")

    @model_validator(mode="after")
    def pdf_pages_use_direct_fetch(self) -> JinaWebFetchArguments:
        if self.pdf_pages and self.model_fields_set - WebFetchArguments.model_fields.keys():
            raise ValueError("Reader options cannot be combined with direct PDF page rendering")
        return self


class DownloadFileArguments(ToolArguments):
    url: str = Field(min_length=1, max_length=1_200)
    path: str | None = Field(default=None, min_length=1, max_length=1_000)


class FetchTranscriptArguments(ToolArguments):
    url: str = Field(
        min_length=1,
        max_length=1_200,
        description=(
            "Complete remote media page URL. Pass a URL, not a video ID, caption endpoint, "
            "attachment ID, or file path."
        ),
    )
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
        description=(
            "Preferred existing caption language as a BCP 47 tag, for example de or en-US. "
            "Omit unless the user requests a language. If unavailable, the host selects the "
            "original language; this never translates or transcribes media."
        ),
    )


class GenerateImageArguments(ToolArguments):
    prompt: str = Field(min_length=1, max_length=20_000)
    width: int | None = Field(default=None, ge=64, le=4_096)
    height: int | None = Field(default=None, ge=64, le=4_096)
    seed: int | None = Field(default=None, ge=0)


class WifiQrArguments(ToolArguments):
    ssid: str | ProtectedText
    password: str | ProtectedText | None = None
    security: str | ProtectedText = "WPA"
    hidden: bool = False


class GenerateQrCodeArguments(ToolArguments):
    content: Annotated[str, Field(min_length=1, max_length=2_000)] | ProtectedText | None = None
    wifi: WifiQrArguments | None = None

    @model_validator(mode="after")
    def choose_payload(self) -> GenerateQrCodeArguments:
        if (self.content is None) == (self.wifi is None):
            raise ValueError("provide QR content or Wi-Fi credentials")
        return self

    @field_validator("content")
    @classmethod
    def content_fits_qr_code(cls, value: str | ProtectedText | None) -> str | ProtectedText | None:
        if isinstance(value, str) and len(value.encode("utf-8")) > QR_CODE_MAX_CONTENT_BYTES:
            raise ValueError("content exceeds the QR code byte limit")
        return value


class FileReference(ToolArguments):
    root: Literal["session", "workspace"]
    path: str = Field(min_length=1, max_length=2_000)


class ReadQrCodeArguments(FileReference):
    pass


class EditImageArguments(ToolArguments):
    prompt: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Standalone edit contract. Put the base edit target first as Image 1, identify every "
            "other input only by its reference role, and state the requested delta before the "
            "invariants. For localized edits, keep Image 1 as the same scene and preserve its canvas, "
            "crop, framing, camera, perspective, geometry, identity, and unrelated content. Quote "
            "visible text that must remain verbatim."
        ),
    )
    images: list[FileReference] = Field(
        min_length=1,
        max_length=5,
        description=(
            "One to five image files, ordered as Image 1, "
            "Image 2, and so on."
        ),
    )

    @field_validator("images")
    @classmethod
    def unique_images(cls, value: list[FileReference]) -> list[FileReference]:
        if len({(item.root, item.path) for item in value}) != len(value):
            raise ValueError("images must be unique")
        return value


class GenerateSpeechArguments(ToolArguments):
    text: str = Field(min_length=1, max_length=100_000)
    voice: str | None = Field(default=None, min_length=1, max_length=500)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class GenerateAudioArguments(ToolArguments):
    prompt: str = Field(min_length=1, max_length=20_000)
    duration_seconds: float | None = Field(default=None, gt=0, le=3_600)
    lyrics: str | None = Field(default=None, max_length=100_000)
    vocal_language: str | None = Field(default=None, min_length=2, max_length=35)
    seed: int | None = Field(default=None, ge=0)


class TranscribeAudioArguments(FileReference):
    language: str | None = Field(default=None, min_length=2, max_length=35)


class AnalyzeVideoArguments(FileReference):
    language: str | None = Field(default=None, min_length=2, max_length=35)
    analysis_mode: Literal["speech", "overview", "fine_grained"] = Field(
        default="overview",
        validation_alias=AliasChoices("analysis_mode", "detail"),
        description=(
            "speech for spoken content; overview for visual summaries or chronology; fine_grained for "
            "exact visual steps or troubleshooting."
        ),
    )
    focus: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_000,
        description="Requested deliverable or question.",
    )

    @field_validator("analysis_mode", mode="before")
    @classmethod
    def migrate_detail_names(cls, value: Any) -> Any:
        return {"standard": "overview", "detailed": "fine_grained"}.get(value, value)


class AnalyzeAudioArguments(FileReference):
    language: str | None = Field(default=None, min_length=2, max_length=35)
    analysis_mode: Literal["speech", "overview", "fine_grained"] = Field(
        default="overview",
        description=(
            "speech returns transcript segments only; overview summarizes audible content; "
            "fine_grained locates passages or describes events with estimated timestamps. "
            "overview and fine_grained require a native audio chat model."
        ),
    )
    focus: str | None = Field(
        default=None, min_length=1, max_length=2_000,
        description="Question or search objective, including approximate words or sung lyrics.",
    )


class SessionReadArguments(ToolArguments):
    session_id: UUID | None = Field(
        default=None,
        description="Target session UUID. Omit only to read the current session.",
    )
    after_message_id: UUID | None = Field(
        default=None,
        description="Latest nextAfterMessageId for this session; never use a message ID.",
    )
    around_message_id: UUID | None = Field(
        default=None,
        description="Use an ID from session discovery or compacted history; never invent it.",
    )
    limit: int = Field(default=30, ge=1, le=50)

    @model_validator(mode="after")
    def one_position(self) -> SessionReadArguments:
        if self.after_message_id is not None and self.around_message_id is not None:
            raise ValueError("after_message_id and around_message_id are mutually exclusive")
        return self


class SessionToolResultReadArguments(ToolArguments):
    message_id: UUID = Field(description="Copy the current-session tool-message UUID from toolResultReference.arguments.message_id or session_read's tool message id. Never use a toolCallId or resource ID.")
    offset: int = Field(default=0, ge=0)


class SessionSearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    include_private: bool = False
    limit: int = Field(default=10, ge=1, le=20)


class ContextSearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=2_000)
    include_private: bool = False
    scope: Literal["all", "memories", "sessions"] = "all"
    session_id: UUID | None = Field(
        default=None, description="Restrict search to this known conversation; searches sessions only.",
    )
    limit: int = Field(default=3, ge=1, le=10, description="Maximum results per source.")

    @model_validator(mode="after")
    def validate_scope(self) -> ContextSearchArguments:
        if self.session_id is not None and self.scope == "memories":
            raise ValueError("session_id cannot restrict memory search")
        if not self.query.strip():
            raise ValueError("query must not be blank")
        return self


class MemoryBrowseArguments(ToolArguments):
    category: MemoryCategory | None = None
    cursor_updated_at: datetime | None = None
    cursor_id: UUID | None = None
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def validate_cursor(self) -> MemoryBrowseArguments:
        if (self.cursor_updated_at is None) != (self.cursor_id is None):
            raise ValueError("both cursor fields are required together")
        return self


class SessionBrowseArguments(ToolArguments):
    include_private: bool = False
    limit: int = Field(default=10, ge=1, le=20)


class MemoryWriteArguments(ToolArguments):
    action: Literal["create", "update", "forget"]
    memory_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)
    category: MemoryCategory | None = Field(
        default=None,
        description=(
            "Use exactly one of context, fact, goal, identity, or preference. "
            "Names and personal identity use identity; occupations use fact."
        )
    )
    pin: bool = Field(
        default=False,
        description=(
            "Set true when the latest user explicitly asks to always consider this memory "
            "in future answers, including answers about a stated topic. Plain remember or "
            "store wording stays false. Example: 'Always consider this for meal suggestions' "
            "means true. False does not unpin."
        ),
    )

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be blank")
        return cleaned

    @model_validator(mode="after")
    def memory_id_matches_action(self) -> MemoryWriteArguments:
        if self.action in {"update", "forget"} and self.memory_id is None:
            raise ValueError("memory_id is required for updates and forgetting")
        if self.action in {"update", "forget"} and self.expected_revision is None:
            raise ValueError("expected_revision is required for updates and forgetting")
        if self.action == "create":
            self.memory_id = None
            self.expected_revision = None
        if self.action in {"create", "update"} and (
            self.content is None or self.category is None
        ):
            raise ValueError("content and category are required for creates and updates")
        if self.action == "forget":
            if self.pin:
                raise ValueError("pin is not allowed for forgetting")
            self.content = None
            self.category = None
        return self


class MemoryRetrieveArguments(ToolArguments):
    purpose: Literal["context", "write"] = Field(
        default="context",
        description="Use write when this lookup prepares a memory_write; otherwise context.",
    )
    mode: Literal["search", "browse"] = Field(
        description="Required: pass search with query for a topic, or browse without query for an overview."
    )
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_000,
        description="Required for search and forbidden for browse.",
    )
    category: MemoryCategory | None = Field(
        default=None, description="Optional memory-category filter."
    )
    cursor_updated_at: datetime | None = Field(
        default=None,
        description="Timestamp from nextCursor; pair with cursor_id.",
    )
    cursor_id: UUID | None = Field(
        default=None,
        description="Memory ID from nextCursor; pair with cursor_updated_at.",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum returned items.")

    @model_validator(mode="after")
    def query_matches_mode(self) -> MemoryRetrieveArguments:
        if self.mode == "search" and self.query is None:
            raise ValueError("query is required for search mode")
        if self.mode == "browse" and self.query is not None:
            raise ValueError("query is not allowed for browse mode")
        if (self.cursor_updated_at is None) != (self.cursor_id is None):
            raise ValueError("both cursor fields are required together")
        if self.mode == "search" and self.cursor_updated_at is not None:
            raise ValueError("cursor is only allowed for browse mode")
        return self


class UpdateAgentIdentityArguments(ToolArguments):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    set_as_default: bool | None = Field(
        default=None, strict=True,
        description="True makes the active or newly created personal persona the account default. False clears the personal default. Omit to keep it unchanged.",
    )
    instructions: str | None = Field(
        default=None,
        max_length=16_000,
        description=(
            "Copy explicit user-supplied persona instructions exactly. Do not paraphrase, "
            "translate, strengthen, or add qualifiers."
        ),
    )

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("display_name must not be null")
        return normalize_agent_display_name(value)

    @field_validator("instructions")
    @classmethod
    def normalize_instructions(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("instructions must not be null")
        return value.strip()

    @model_validator(mode="after")
    def require_change(self) -> UpdateAgentIdentityArguments:
        if self.display_name is None and self.instructions is None and self.set_as_default is None:
            raise ValueError("at least one identity field is required")
        return self


class UpdateUserProfileArguments(ToolArguments):
    model_scope: Literal[
        "current_model", "private_models", "public_models", "both_models"
    ] = Field(
        default="current_model",
        description=(
            "Target current_model unless the user explicitly requests private, public, or both models."
        ),
    )
    full_name: str | None = Field(
        default=None,
        max_length=20_000,
        description="The user's complete name, not the name used to address them.",
    )
    preferred_name: str | None = Field(
        default=None,
        max_length=20_000,
        description="The name to use when addressing the user.",
    )
    form_of_address: str | None = Field(default=None, max_length=20_000)
    pronouns: str | None = Field(default=None, max_length=20_000)
    primary_language: str | None = Field(default=None, max_length=20_000)
    primary_residence: str | None = Field(default=None, max_length=20_000)

    @field_validator(
        "full_name",
        "preferred_name",
        "form_of_address",
        "pronouns",
        "primary_language",
        "primary_residence",
    )
    @classmethod
    def normalize_profile_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not (cleaned := value.strip()):
            raise ValueError("profile values must not be blank")
        return cleaned

    @model_validator(mode="after")
    def require_change(self) -> UpdateUserProfileArguments:
        if not self.model_fields_set.intersection(
            {
                "full_name",
                "preferred_name",
                "form_of_address",
                "pronouns",
                "primary_language",
                "primary_residence",
            }
        ):
            raise ValueError("at least one profile field is required")
        return self


class PersonalSettingsArguments(ToolArguments):
    action: Literal["list", "get", "update", "create", "delete"]
    area: Literal["memory", "user_profile", "agent_identity", "integrations", "variables"] | None = None
    changes: dict[str, Any] | None = Field(default=None, max_length=20)
    model_scope: Literal[
        "current_model", "private_models", "public_models", "both_models"
    ] = "current_model"
    integration_id: UUID | None = None
    adapter_type: str | None = None
    variable_id: UUID | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> PersonalSettingsArguments:
        if self.action == "list":
            if self.changes is not None:
                raise PydanticCustomError("personal_settings_invalid", "Changes are not allowed for list", {"field": "changes"})
            if self.integration_id is not None:
                raise PydanticCustomError("personal_settings_invalid", "Integration IDs are not allowed for list", {"field": "integration_id"})
        elif self.area is None:
            raise PydanticCustomError("personal_settings_invalid", "Area is required", {"field": "area"})
        elif self.action in {"create", "delete"} and self.area not in {"integrations", "variables"}:
            raise ValueError("only integrations and variables can be created or deleted")
        elif self.action in {"create", "update"} and not self.changes:
            raise PydanticCustomError("personal_settings_invalid", "Changes are required", {"field": "changes"})
        elif self.action in {"get", "delete"} and self.changes is not None:
            raise PydanticCustomError("personal_settings_invalid", "Changes are not allowed", {"field": "changes"})
        if self.integration_id is not None and self.area != "integrations":
            raise ValueError("integration_id requires the integrations area")
        if self.area == "integrations" and self.action in {"update", "delete"} and self.integration_id is None and self.adapter_type != "search_selection":
            raise ValueError("integration_id is required")
        if self.adapter_type == "search_selection" and self.action not in {"get", "update"}:
            raise ValueError("search selection can only be read or updated")
        if self.adapter_type == "caldav_defaults" and (self.action not in {"get", "update"} or self.integration_id is None):
            raise ValueError("CalDAV defaults require an integration")
        if self.action == "create" and (self.integration_id is not None or self.variable_id is not None):
            raise ValueError("integration_id is assigned by the server")
        if self.adapter_type is not None and self.area != "integrations":
            raise ValueError("adapter_type requires integrations")
        if self.area == "integrations" and self.action == "create" and self.adapter_type is None:
            raise ValueError("adapter_type is required")
        if self.variable_id is not None and self.area != "variables":
            raise ValueError("variable_id requires variables")
        if self.area == "variables" and self.action in {"update", "delete"} and self.variable_id is None:
            raise ValueError("variable_id is required")
        if self.model_scope != "current_model" and self.area != "user_profile":
            raise ValueError("model_scope applies only to the user profile")
        return self


class SaveSkillArguments(ToolArguments):
    scope: Literal["personal", "instance"] = Field(default="personal", description="Global requires explicit request and admin rights.")
    path: str = Field(min_length=1, max_length=500)
    skill_id: UUID | None = Field(default=None, description="Omit to create.")
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", description="Base revision for updates.")
    executable_paths: list[str] = Field(default_factory=list, max_length=256, description="Executable package-relative paths.")

    @model_validator(mode="after")
    def require_update_revision(self) -> SaveSkillArguments:
        if (self.skill_id is None) != (self.expected_revision is None):
            raise ValueError("skill_id and expected_revision must be supplied together")
        return self


class EditSkillArguments(ToolArguments):
    scope: Literal["personal", "instance"] = "personal"
    skill_id: UUID = Field(description="Exact catalog ID.")


class LoadSkillArguments(ToolArguments):
    skill_id: UUID = Field(description="Exact catalog ID.")
    on_computer: bool = Field(default=False, description="Prepare package files on the selected computer; reload after switching computers.")


class AutomationScheduleArguments(ToolArguments):
    cron: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    timezone: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    run_at: datetime | None = None
    valid_until: datetime | None = Field(
        default=None,
        description="Cron only; not with run_at.",
    )
    enabled: bool = True

    @field_validator("cron", "timezone")
    @classmethod
    def normalize_schedule_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not (cleaned := value.strip()):
            raise ValueError("schedule value must not be blank")
        return cleaned

    @model_validator(mode="after")
    def schedule_is_valid(self) -> AutomationScheduleArguments:
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None
        ):
            raise ValueError("valid_until must be timezone-aware")
        if self.run_at is None:
            if self.cron is None or self.timezone is None:
                raise ValueError("cron and timezone are required together")
            next_run_at(self.cron, self.timezone, datetime.now(UTC))
        elif (
            self.cron is not None
            or self.valid_until is not None
            or self.run_at.tzinfo is None
            or self.run_at.utcoffset() is None
        ):
            raise ValueError(
                "run_at must be timezone-aware and cannot use recurrence fields"
            )
        else:
            self.timezone = None
        return self


class AutomationDefinitionArguments(ToolArguments):
    name: str = Field(min_length=1, max_length=240)
    task: str = Field(min_length=1, max_length=40_000)
    enabled: bool = True
    schedules: list[AutomationScheduleArguments] = Field(
        max_length=50, description="Empty when no time-based starts are needed."
    )
    triggers: list[Literal["agent_response", "agent_error"]] = Field(
        default_factory=list, max_length=2,
        description="Events from the owner's non-automation agent runs. Put optional conditions in task.",
    )

    @field_validator("name", "task")
    @classmethod
    def normalize_automation_text(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("automation text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def schedules_are_unique(self) -> AutomationDefinitionArguments:
        keys = [
            ("once", item.run_at)
            if item.run_at is not None
            else ("cron", item.cron, item.timezone)
            for item in self.schedules
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("automation schedules must be unique")
        return self


class ListAutomationsArguments(ToolArguments):
    scope: Literal["session", "all"] = Field(default="session", description="Current session by default; all lists the owner's automations across sessions.")
    limit: int = Field(default=20, ge=1, le=50)


class GetAutomationArguments(ToolArguments):
    automation_id: UUID


class CreateAutomationArguments(AutomationDefinitionArguments):
    pass


class EditAutomationArguments(AutomationDefinitionArguments):
    automation_id: UUID
    expected_revision: int = Field(ge=1)


class RunAutomationArguments(ToolArguments):
    automation_id: UUID
    expected_revision: int = Field(ge=1)


class DeleteAutomationArguments(RunAutomationArguments):
    pass


class CodingAnalyzeArguments(ToolArguments):
    title: str = Field(min_length=1, max_length=240)
    brief: str = Field(min_length=1, max_length=50_000)


class CodingCreatePlanArguments(ToolArguments):
    title: str = Field(min_length=1, max_length=240)
    markdown: str = Field(min_length=1, max_length=500_000)
    external_session_id: str | None = Field(default=None, min_length=1, max_length=300)


class CodingListPlansArguments(ToolArguments):
    limit: int = Field(default=20, ge=1, le=50)


class CodingEnqueueArguments(ToolArguments):
    plan_ids: list[UUID] = Field(min_length=1, max_length=100)
