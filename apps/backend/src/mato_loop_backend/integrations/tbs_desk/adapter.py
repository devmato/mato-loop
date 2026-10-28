from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolContribution,
)
from mato_loop_backend.integrations.service import _load_integrations
from mato_loop_backend.integrations.tbs_desk.client import (
    TbsDeskClient,
    TbsDeskError,
    TbsErpCustomer,
    TbsTicketDetail,
    TbsTicketSummary,
)
from mato_loop_backend.security import (
    delete_credential,
    load_credential,
    read_credential,
    store_credential,
)
from mato_loop_backend.settings import (
    AgentProfile,
    IntegrationInstance,
    SettingsStore,
    UserIntegrationSettings,
)
from mato_loop_backend.tools.actions import ToolAction

if TYPE_CHECKING:
    from mato_loop_backend.tools.context import ToolContext
    from mato_loop_backend.tools.contracts import ToolResult

TBS_DESK_CREDENTIAL_KIND = "tbs_desk.password"
_READ = ToolAction("support.read", "Supportdaten lesen", category="read")


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class TbsDeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl
    username: str = Field(min_length=1, max_length=320)

    @model_validator(mode="after")
    def valid_endpoint(self) -> TbsDeskConfig:
        if (
            self.base_url.username is not None
            or self.base_url.password is not None
            or self.base_url.query is not None
            or self.base_url.fragment is not None
        ):
            raise ValueError("tbs.Desk requires an endpoint without credentials")
        return self


class TbsCustomerState(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")

    id: int = Field(ge=1)
    erp_id: int | None = Field(default=None, ge=1)
    display_name: str = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=750)
    active_call: bool = False
    contract: str | None = Field(default=None, max_length=500)
    zipcode: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=500)


class TbsTicketState(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")

    id: int = Field(ge=1)
    header: str = Field(default="", max_length=2_000)
    created: str | None = Field(default=None, max_length=100)
    open_from: str | None = Field(default=None, max_length=100)
    state: int | None = None
    priority: int | None = None
    entry_count: int | None = Field(default=None, ge=0)
    attachment_count: int | None = Field(default=None, ge=0)


class TbsSupportState(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")

    integration_id: UUID
    binding_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[TbsCustomerState] = Field(default_factory=list, max_length=12)
    selected_customer: TbsCustomerState | None = None
    tickets: list[TbsTicketState] = Field(default_factory=list, max_length=20)


class SelectCustomerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, min_length=2, max_length=200)
    erp_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def one_action(self) -> SelectCustomerArguments:
        if self.query is not None and self.erp_id is not None:
            raise ValueError("query and erp_id cannot be combined")
        return self

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str | None) -> str | None:
        if value is not None and len(value := value.strip()) < 2:
            raise ValueError("query must contain at least two characters")
        return value


class SearchTicketsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=20, ge=1, le=20)


class ReadTicketArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: int = Field(ge=1)


def profile_id(integration_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"matoloop:tbs-desk:{integration_id}")


def binding_revision(instance: IntegrationInstance) -> str:
    payload = json.dumps(
        instance.binding_data(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _profile(instance: IntegrationInstance) -> AgentProfile:
    return AgentProfile(
        id=profile_id(instance.id),
        name="Support-Assistent",
        description="Hilft bei der Diagnose und Lösung von Supportfällen aus tbs.Desk.",
        instructions=(
            "You assist a support employee with customer cases. Treat short problem reports as customer cases "
            "unless the user explicitly says otherwise; do not reinterpret them as the support employee's "
            "personal issue. Before diagnosing a case, resolve its customer. An unfamiliar standalone word "
            "may be a customer name: extract a concise possible customer name or number from the user's "
            "unstructured message and call tbs_desk_select_customer with query. If there is no customer hint, "
            "call the tool without query to check the active call. Ask for a customer name or number only when "
            "that lookup cannot identify a customer. When the tool returns candidates, stop and let the user "
            "use the displayed selection table; do not repeat the table. A later user message names the ERP "
            "customer number; call the same tool with erp_id. Keep the selected customer for follow-up messages "
            "unless the user requests a change, then search this customer's tickets with concise problem terms. "
            "Use tickets as case history. Distinguish documented observations from hypotheses, propose concrete "
            "diagnostic steps, and ask the support user directly for missing observations. Do not offer "
            "communication or changes without a tool. Never treat the support user as the customer and never "
            "search memories or prior chats to guess a customer."
        ),
        integrations_enabled_by_default=False,
        integration_id=instance.id,
        editable=False,
    )


def support_state(session: Any) -> TbsSupportState | None:
    if session.support_state is None:
        return None
    try:
        return TbsSupportState.model_validate(session.support_state)
    except Exception as exc:
        raise TbsDeskError("integration.invalidResponse") from exc


def set_support_state(session: Any, state: TbsSupportState | None) -> None:
    session.support_state = (
        state.model_dump(mode="json", by_alias=True) if state is not None else None
    )


def _customer_state(
    customer: TbsErpCustomer, *, label: str | None = None, active_call: bool = False
) -> TbsCustomerState:
    return TbsCustomerState(
        id=customer.erp_id,
        erp_id=customer.erp_id,
        display_name=customer.displayname,
        label=label,
        active_call=active_call,
        contract=customer.contract,
        zipcode=customer.zipcode,
        city=customer.city,
    )


def _ticket_state(ticket: TbsTicketSummary) -> TbsTicketState:
    return TbsTicketState.model_validate(ticket.model_dump(exclude={"customer"}))


def _active_instance(context: ToolContext) -> IntegrationInstance:
    instances = [
        item
        for item in context.integration_settings.instances
        if item.enabled
        and item.integration_type == "support"
        and item.adapter_type == "tbs-desk"
        and item.owner_id == context.session.user_id
    ]
    if len(instances) != 1:
        raise TbsDeskError("tool.unavailable")
    return instances[0]


def _credential(record: Any) -> str:
    if record is None or record.kind != TBS_DESK_CREDENTIAL_KIND:
        raise TbsDeskError("integration.authenticationFailed")
    try:
        payload = read_credential(record)
    except Exception as exc:
        raise TbsDeskError("integration.authenticationFailed") from exc
    if set(payload) != {"password"} or not isinstance(payload["password"], str):
        raise TbsDeskError("integration.authenticationFailed")
    return payload["password"]


@asynccontextmanager
async def selected_client(context: ToolContext):
    instance = _active_instance(context)
    if instance.credential_id is None:
        raise TbsDeskError("integration.authenticationFailed")
    record = await load_credential(
        context.db, instance.credential_id, owner_id=context.session.user_id
    )
    config = TbsDeskConfig.model_validate(instance.config)
    async with TbsDeskClient(
        str(config.base_url), config.username, _credential(record)
    ) as client:
        yield instance, client


async def _save_state(context: ToolContext, state: TbsSupportState) -> None:
    set_support_state(context.session, state)
    await context.db.commit()


def _markdown_cell(value: str | None) -> str:
    return re.sub(r"([\\`*_[\]<>|])", r"\\\1", (value or "").replace("\n", " ")) or "-"


def _customer_reply(candidates: list[TbsCustomerState]) -> dict[str, Any]:
    rows = ["| Kundennummer | Kunde | Ort |", "|---|---|---|"]
    options = []
    for candidate in candidates:
        reply_id = f"customer-{candidate.erp_id}"
        number = f"[{candidate.erp_id}](#reply-{reply_id})"
        name = _markdown_cell(candidate.display_name)
        if candidate.active_call:
            name += " (Aktiver Anruf)"
        place = _markdown_cell(" ".join(filter(None, (candidate.zipcode, candidate.city))))
        rows.append(f"| {number} | {name} | {place} |")
        options.append({
            "id": reply_id,
            "content": f"Verwende ERP-Kunde {candidate.erp_id}.",
        })
    return {"markdown": "\n".join(rows), "options": options}


async def tbs_desk_select_customer(
    arguments: SelectCustomerArguments, context: ToolContext
) -> ToolResult:
    from mato_loop_backend.tools.contracts import ToolResult

    async with selected_client(context) as (instance, client):
        current = support_state(context.session)
        if arguments.erp_id is not None:
            if (
                current is None
                or current.integration_id != instance.id
                or current.binding_revision != binding_revision(instance)
            ):
                return ToolResult.error("tbsDesk.customerNotFound")
            selected = next(
                (item for item in current.candidates if item.erp_id == arguments.erp_id),
                None,
            )
            if selected is None:
                return ToolResult.error("tbsDesk.customerNotFound")
            current.selected_customer = selected
            current.tickets = []
            await _save_state(context, current)
            return ToolResult.json({
                "selectedCustomer": selected.model_dump(mode="json", by_alias=True)
            })

        query = arguments.query
        candidates: dict[int, tuple[TbsErpCustomer, bool]] = {}
        if query is None:
            active = await client.active_call_customer()
            if active is not None and active.erp_id is not None:
                candidates[active.erp_id] = (
                    TbsErpCustomer(erp_id=active.erp_id, displayname=active.displayname),
                    True,
                )
        if query is None and not candidates:
            state = TbsSupportState(
                integration_id=instance.id,
                binding_revision=binding_revision(instance),
            )
            await _save_state(context, state)
            return ToolResult.json({
                "selectionRequired": False,
                "candidates": [],
                "nextAction": "askForCustomer",
            })
        if query is not None:
            for customer in await client.search_customers(query):
                _previous, is_active = candidates.get(
                    customer.erp_id, (customer, False)
                )
                candidates[customer.erp_id] = (customer, is_active)
        choices = list(candidates.values())[:12]
        labels = {
            item.erp_id: f"{item.displayname} (Kunde {item.erp_id})"
            for item, _active in choices
        }
        state = TbsSupportState(
            integration_id=instance.id,
            binding_revision=binding_revision(instance),
            candidates=[
                _customer_state(
                    item, label=labels[item.erp_id], active_call=is_active
                )
                for item, is_active in choices
            ],
        )
        await _save_state(context, state)
        if not choices:
            return ToolResult.json({
                "selectionRequired": False,
                "query": query,
                "candidates": [],
                "nextAction": "askForDifferentCustomer",
            })
        return ToolResult.json(
            {
                "selectionRequired": True,
                "query": query,
                "candidates": [
                    item.model_dump(mode="json", by_alias=True) for item in state.candidates
                ],
            },
            metadata={"preparedReply": _customer_reply(state.candidates)},
        )


def _selected_state(context: ToolContext, instance: IntegrationInstance) -> TbsSupportState:
    state = support_state(context.session)
    if (
        state is None
        or state.integration_id != instance.id
        or state.binding_revision != binding_revision(instance)
        or state.selected_customer is None
        or state.selected_customer.erp_id is None
    ):
        raise TbsDeskError("tbsDesk.customerRequired")
    return state


async def tbs_desk_search_tickets(
    arguments: SearchTicketsArguments, context: ToolContext
) -> ToolResult:
    from mato_loop_backend.tools.contracts import ToolResult

    async with selected_client(context) as (instance, client):
        state = _selected_state(context, instance)
        tickets = await client.search_tickets(
            state.selected_customer.erp_id,
            query=arguments.query.strip() if arguments.query else None,
            limit=arguments.limit,
        )
        state.tickets = [_ticket_state(ticket) for ticket in tickets]
        await _save_state(context, state)
        return ToolResult.json({
            "customer": state.selected_customer.model_dump(mode="json", by_alias=True),
            "tickets": [item.model_dump(mode="json", by_alias=True) for item in state.tickets],
        })


async def read_ticket(
    client: TbsDeskClient, ticket_id: int, erp_id: int
) -> TbsTicketDetail:
    matches = await client.search_tickets(
        erp_id, query=None, limit=1, ticket_id=ticket_id
    )
    if len(matches) != 1 or matches[0].id != ticket_id:
        raise TbsDeskError("tbsDesk.ticketCustomerMismatch")
    ticket = await client.ticket(ticket_id)
    if ticket.customer_id != matches[0].customer.id:
        raise TbsDeskError("tbsDesk.ticketCustomerMismatch")
    return ticket


async def tbs_desk_read_ticket(
    arguments: ReadTicketArguments, context: ToolContext
) -> ToolResult:
    from mato_loop_backend.tools.contracts import ToolResult

    async with selected_client(context) as (instance, client):
        state = _selected_state(context, instance)
        ticket = await read_ticket(client, arguments.ticket_id, state.selected_customer.erp_id)
        return ToolResult.json(ticket.model_dump(mode="json"))


def _permission(_arguments: BaseModel, context: ToolContext) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionRequest

    instance = _active_instance(context)
    return (PermissionRequest(
        capability=_READ.capability,
        resource_type="network_service",
        resource_scope=str(instance.id),
        resource_display=instance.name,
        action_kind="tbs_desk_read",
        risk_level="medium",
        title="Supportdaten lesen?",
        summary="Der Agent möchte Kundendaten und Tickets aus tbs.Desk lesen.",
        policy_integration_id=instance.id,
    ),)


TBS_DESK_TOOLS = (
    ToolContribution(
        name="tbs_desk_select_customer",
        description="Select the customer for this session before searching or reading tickets.",
        arguments_model=SelectCustomerArguments,
        handler=tbs_desk_select_customer,
        label="Support-Kunden auswählen",
        icon="support_agent",
        actions=(_READ,),
        authorization_factory=_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="tbs_desk_search_tickets",
        description="Search tickets for the server-selected customer. Customer IDs are host-bound.",
        arguments_model=SearchTicketsArguments,
        handler=tbs_desk_search_tickets,
        label="Support-Tickets suchen",
        icon="search",
        actions=(_READ,),
        authorization_factory=_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="tbs_desk_read_ticket",
        description="Read one ticket only when it belongs to the server-selected customer.",
        arguments_model=ReadTicketArguments,
        handler=tbs_desk_read_ticket,
        label="Support-Ticket lesen",
        icon="confirmation_number",
        actions=(_READ,),
        authorization_factory=_permission,
        releases_private_data=True,
    ),
)


TBS_DESK_ADAPTER = IntegrationAdapterDefinition(
    integration_type="support",
    adapter_type="tbs-desk",
    config_model=TbsDeskConfig,
    contribution_factory=lambda instance: IntegrationContributions(
        agent_profiles=(_profile(instance),)
    ),
    resource_actions=(_READ,),
    shared_tools=TBS_DESK_TOOLS,
    personal_only=True,
)


class TbsDeskIntegrationService:
    @staticmethod
    def find(settings: UserIntegrationSettings, integration_id: UUID):
        return next((item for item in settings.instances if item.id == integration_id
                     and item.integration_type == "support"
                     and item.adapter_type == "tbs-desk"), None)

    @classmethod
    async def create(cls, db, *, owner_id: UUID, name: str, base_url: str,
                     username: str, password: str, enabled: bool):
        if not password:
            raise TbsDeskError("validation.required")
        settings = await SettingsStore.load(
            db, UserIntegrationSettings, user_id=owner_id, for_update=True
        )
        instance = IntegrationInstance(
            name=name, integration_type="support", adapter_type="tbs-desk",
            enabled=enabled, config={"base_url": base_url.rstrip("/"), "username": username},
        )
        instance.owner_id = owner_id
        TbsDeskConfig.model_validate(instance.config)
        credential = await store_credential(
            db, owner_id=owner_id, kind=TBS_DESK_CREDENTIAL_KIND,
            label=name, payload={"password": password},
        )
        instance.credential_id = credential.id
        settings.instances.append(instance)
        await SettingsStore.save(db, settings, user_id=owner_id)
        await db.commit()
        return settings

    @classmethod
    async def update(cls, db, integration_id: UUID, *, owner_id: UUID, name: str,
                     base_url: str, username: str, password: str | None, enabled: bool,
                     expected_instance: IntegrationInstance | None = None):
        settings = await _load_integrations(
            db, owner_id, for_update=True, expected_instance=expected_instance,
        )
        instance = cls.find(settings, integration_id)
        if instance is None:
            raise TbsDeskError("integration.notFound")
        config = {"base_url": base_url.rstrip("/"), "username": username}
        connection_changed = instance.config != config
        old_credential_id = instance.credential_id
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        instance.owner_id = owner_id
        TbsDeskConfig.model_validate(instance.config)
        if password is not None:
            credential = await store_credential(
                db, owner_id=owner_id, kind=TBS_DESK_CREDENTIAL_KIND,
                label=name, payload={"password": password},
            )
            instance.credential_id = credential.id
            if old_credential_id is not None:
                await delete_credential(db, old_credential_id, owner_id=owner_id)
        if connection_changed:
            from mato_loop_backend.integrations.service import (
                _clear_integration_policies,
                _load_tools,
            )

            tools = await _load_tools(db, owner_id, for_update=True)
            _clear_integration_policies(instance)
            if tools.resource_labels.pop(str(integration_id), None) is not None:
                await SettingsStore.save(db, tools, user_id=owner_id)
        await SettingsStore.save(db, settings, user_id=owner_id)
        await db.commit()
        return settings

    @classmethod
    async def delete(cls, db, integration_id: UUID, *, owner_id: UUID):
        settings = await SettingsStore.load(
            db, UserIntegrationSettings, user_id=owner_id, for_update=True
        )
        instance = cls.find(settings, integration_id)
        if instance is None:
            raise TbsDeskError("integration.notFound")
        settings.instances.remove(instance)
        if instance.credential_id is not None:
            await delete_credential(db, instance.credential_id, owner_id=owner_id)
        from mato_loop_backend.integrations.service import _load_tools

        tools = await _load_tools(db, owner_id, for_update=True)
        if tools.resource_labels.pop(str(integration_id), None) is not None:
            await SettingsStore.save(db, tools, user_id=owner_id)
        await SettingsStore.save(db, settings, user_id=owner_id)
        await db.commit()
        return settings
