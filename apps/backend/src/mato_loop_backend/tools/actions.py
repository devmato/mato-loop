from dataclasses import KW_ONLY, dataclass
from typing import Literal, get_args

from mato_loop_backend.settings.models import ToolExecutionPolicy, ToolSettings

ToolActionCategory = Literal["read", "read_sensitive", "write", "delete"]


@dataclass(frozen=True, slots=True)
class ToolAction:
    capability: str
    label: str
    _: KW_ONLY
    category: ToolActionCategory
    default: ToolExecutionPolicy | None = None
    configurable: bool = True
    # Keep execution retry accounting independent of preset categorization.
    mutates: bool = False
    allowed_policies: tuple[ToolExecutionPolicy, ...] = tuple(ToolExecutionPolicy)

    def __post_init__(self) -> None:
        if self.category not in get_args(ToolActionCategory):
            raise ValueError("invalid tool action category")
        if self.default is not None and self.default not in self.allowed_policies:
            raise ValueError("invalid tool action default")

    def default_policy(self, settings: ToolSettings) -> ToolExecutionPolicy:
        from mato_loop_backend.settings.tool_presets import preset_policy

        policy = self.default if self.default is not None else preset_policy(
            getattr(settings, f"{settings.trust_level.value}_permission_preset"), self.category, self.capability,
            profile=settings._agent_profile,
        )
        if policy in self.allowed_policies:
            return policy
        return ToolExecutionPolicy.ASK if ToolExecutionPolicy.ASK in self.allowed_policies else ToolExecutionPolicy.DISABLED


@dataclass(frozen=True, slots=True)
class ApprovalEditField:
    path: tuple[str, ...]
    label: str
    input_type: Literal["text", "textarea", "temporal"] = "text"
    required: bool = False
    max_length: int = 10_000


@dataclass(frozen=True, slots=True)
class ToolPolicyResolution:
    policy: ToolExecutionPolicy
    source: str
    reason: str


_FILESYSTEM_READ = ToolAction(
    "filesystem.read", "Lesen", category="read"
)

_FILESYSTEM_WRITE = ToolAction(
    "filesystem.write", "Workspace schreiben", category="write", mutates=True
)

_FILESYSTEM_DELETE = ToolAction(
    "filesystem.delete", "Löschen", category="delete", mutates=True
)

_WORKING_DIRECTORY_EXTERNAL = ToolAction(
    "working_directory.external",
    "Zugriff außerhalb des Arbeitsverzeichnisses",
    category="read",
)

# shortcut: this guard also covers confidential writes; narrow it during per-call classification.
_SENSITIVE_DATA_ACCESS = ToolAction(
    "sensitive_data.access",
    "Vertrauliche Pfade und mögliche Secret-Ausgabe",
    category="read_sensitive",
)

_SESSION_FILES_READ = ToolAction(
    "session.files.read", "Session-Dateien lesen", category="read"
)

_FILE_PROVIDE = ToolAction(
    "file.provide", "Datei bereitstellen", category="read"
)

_SESSION_FILES_WRITE = ToolAction(
    "session.files.write", "Session-Dateien schreiben", category="write", mutates=True
)

_SESSION_FILES_DELETE = ToolAction(
    "session.files.delete", "Session-Dateien löschen", category="delete", mutates=True
)

_WEB_SEARCH = ToolAction("network.search", "Websuche", category="read")

_WEB_FETCH = ToolAction("network.fetch", "Webseiten abrufen", category="read")

_TRANSCRIPT_ROUTING_INSTRUCTION = (
    "Published transcript text is evidence of spoken content only, not of visual events. Call "
    "fetch_transcript only when the requested output can be answered from spoken content; never "
    "call it for camera work, visible UI, objects, actions, or other visual details."
)

_SESSION_READ = ToolAction("session.read", "Sessions lesen", category="read")

_SESSION_READ_SENSITIVE = ToolAction(
    "session.read_sensitive", "Private Sessions lesen und durchsuchen", category="read_sensitive"
)

_SESSION_SEARCH = ToolAction(
    "session.search", "Sessions durchsuchen", category="read"
)

_AUTOMATION_READ = ToolAction(
    "automation.read", "Automationen lesen", category="read"
)

_AUTOMATION_CREATE = ToolAction(
    "automation.create",
    "Automationen erstellen",
    category="write",
    mutates=True,
)

_AUTOMATION_UPDATE = ToolAction(
    "automation.update",
    "Automationen ändern",
    category="write",
    mutates=True,
)

_AUTOMATION_RUN = ToolAction(
    "automation.run",
    "Automationen starten",
    category="write",
    mutates=True,
)

_AUTOMATION_DELETE = ToolAction(
    "automation.delete",
    "Automationen löschen",
    category="delete",
    mutates=True,
)

_SESSION_TOOL_RESULT_READ = ToolAction(
    "session.tool_result.read",
    "Historische Tool-Ergebnisse lesen",
    category="read",
)

_MEMORY_READ = ToolAction("memory.read", "Memories lesen", category="read")

# shortcut: this capability includes forgetting; split deletion during per-call classification.
_MEMORY_WRITE = ToolAction(
    "memory.write", "Memories speichern", category="write", mutates=True
)

_SKILL_LOAD = ToolAction("skills.load", "Skills laden", category="read")

_SKILL_WRITE = ToolAction(
    "skills.write", "Skills speichern", category="write", mutates=True
)

_USER_PROFILE_UPDATE = ToolAction(
    "settings.user_profile.update",
    "Persönliches Profil ändern",
    category="write",
    mutates=True,
)

_PERSONAL_SETTINGS_READ = ToolAction(
    "settings.personal.read", "Persönliche Einstellungen lesen",
    category="read",
)

# shortcut: this capability includes deletion; split destructive operations during per-call classification.
_PERSONAL_SETTINGS_WRITE = ToolAction(
    "settings.personal.update", "Persönliche Einstellungen ändern",
    category="write", mutates=True,
)

_SECRETS_ACCESS = ToolAction(
    "secrets.access",
    "Variablen und Secrets verwenden",
    category="read_sensitive",
)

_SECRETS_WRITE = ToolAction(
    "secrets.write",
    "Variablen und Secrets speichern",
    category="write",
    mutates=True,
)

_SHELL_READ = ToolAction("shell.read", "Shell ohne erkannte Schreibaktionen", category="read")

_SHELL_WRITE = ToolAction(
    "shell.write", "Als schreibend eingestufte Shell", category="write", mutates=True
)

_SHELL_DELETE = ToolAction(
    "shell.delete", "Erkannte Shell-Löschaktionen", category="delete", mutates=True
)

_SYSTEM_INFO = ToolAction(
    "system.info", "Systeminformationen", category="read"
)

_EXECUTION_AGENTS_LIST = ToolAction(
    "execution_agent.list", "Computer anzeigen", category="read",
)

_EXECUTION_AGENT_SELECT = ToolAction(
    "execution_agent.select", "Computer übernehmen", category="write", mutates=True,
)

_CODING_ANALYZE = ToolAction(
    "coding.analyze", "Projekt analysieren", category="read"
)

_CODING_PLAN_CREATE = ToolAction(
    "coding.plan.create", "Plan speichern", category="write", mutates=True
)

_CODING_PLAN_READ = ToolAction(
    "coding.plan.read", "Pläne anzeigen", category="read"
)

_CODING_EXECUTE = ToolAction(
    "coding.execute.enqueue", "Pläne ausführen", category="write", mutates=True
)
