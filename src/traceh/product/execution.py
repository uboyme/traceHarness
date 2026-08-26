"""Thin ProductTask execution adapter over the existing Workflow service.

This module is deliberately not another scheduler.  Workflow owns node
progress, Supervisor owns live Agents, and the provisioning boundary owns
Budget and Workspace setup/cleanup.  The adapter keeps only the live binding
needed while this process is actively driving a task; restart decisions are
made from ProductTask and Workflow streams before a binding is rebuilt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol

from traceh.api.agents import AgentSpec, AgentSupervisor
from traceh.api.product import ProductRole
from traceh.api.workflow import WorkflowRun
from traceh.product.assembly import ProductAssembly, ProductPreflight
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.events import require_product_identifier
from traceh.product.topology import (
    PRODUCT_MODE_ROLES,
    product_message_binding,
    product_role_node_id,
    product_spec_binding,
)
from traceh.session.event_store import EventStore
from traceh.supervision.execution import durable_log_identity
from traceh.workflow.models import agent_identity
from traceh.workflow.service import WorkflowService


class ProductTaskProvisioner(Protocol):
    """Prepare and settle the existing resource domains for one task."""

    @property
    def store(self) -> EventStore:
        ...

    async def prepare(self, task_id: str, preflight: ProductPreflight) -> str:
        ...

    async def release(self, task_id: str, *, reason: str) -> None:
        ...

    async def interrupt(self, task_id: str) -> None:
        ...

    async def aclose(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class _RunBinding:
    assembly: ProductAssembly
    owner_agent_id: str
    requirement: str | None


class ProductWorkflowBindingResolver:
    """Resolve the fixed Product topology from one fresh F2 assembly.

    Reports are injected only into later role messages, are bounded before
    interpolation, and contain no Review, approval or promotion values.  They
    are observations of durable Agent reports, never copied into ProductTask.
    """

    __slots__ = ("_bindings", "_lock", "_max_report_chars", "_supervisor")

    def __init__(
        self, supervisor: AgentSupervisor, *, max_report_chars: int
    ) -> None:
        if type(max_report_chars) is not int or max_report_chars < 1:
            raise ProductInputError("product-report-bound-invalid", "max_report_chars")
        self._supervisor = supervisor
        self._max_report_chars = max_report_chars
        self._bindings: dict[str, _RunBinding] = {}
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._supervisor.store

    async def bind(
        self,
        assembly: ProductAssembly,
        *,
        task_id: str,
        owner_agent_id: str,
        requirement: str | None,
    ) -> None:
        task_id = require_product_identifier(task_id, field="task_id")
        owner_agent_id = require_product_identifier(
            owner_agent_id, field="owner_agent_id"
        )
        if requirement is not None and (
            type(requirement) is not str or not requirement.strip()
        ):
            raise ProductInputError("product-requirement-invalid", "requirement")
        candidate = _RunBinding(assembly, owner_agent_id, requirement)
        async with self._lock:
            current = self._bindings.get(task_id)
            if current is not None and (
                current.assembly.receipt.digest != assembly.receipt.digest
                or current.owner_agent_id != owner_agent_id
            ):
                raise ProductStateError("product-execution-binding-conflict", task_id)
            self._bindings[task_id] = candidate

    async def unbind(self, task_id: str) -> None:
        task_id = require_product_identifier(task_id, field="task_id")
        async with self._lock:
            self._bindings.pop(task_id, None)

    async def agent_spec(
        self, binding_id: str, *, run_id: str, node_id: str, map_key: str | None
    ) -> AgentSpec:
        if map_key is not None:
            raise ProductStateError("product-map-binding-unexpected", run_id)
        role = _role_for_binding(binding_id, kind="spec")
        _require_role_node(role, node_id, run_id)
        binding = await self._binding(run_id)
        template = binding.assembly.preflight.profile.assembly(role).spec
        return replace(template, owner_agent_id=binding.owner_agent_id)

    async def message_content(
        self, binding_id: str, *, run_id: str, node_id: str, map_key: str | None
    ) -> str:
        if map_key is not None:
            raise ProductStateError("product-map-binding-unexpected", run_id)
        role = _role_for_binding(binding_id, kind="message")
        _require_role_node(role, node_id, run_id)
        binding = await self._binding(run_id)
        requirement = binding.requirement
        if requirement is None:
            # Clean approval-barrier resume never asks for a message.  Reaching
            # this branch means Workflow tried to re-enter unfinished Agent work,
            # which Stage E intentionally refuses rather than reconstructs.
            raise ProductStateError("product-requirement-unavailable", run_id)
        reports = await self._predecessor_reports(run_id, role)
        return _role_message(role, requirement, reports)

    async def map_keys(
        self, binding_id: str, *, run_id: str, node_id: str
    ) -> tuple[str, ...]:
        del binding_id, node_id
        raise ProductStateError("product-map-binding-unexpected", run_id)

    async def _binding(self, run_id: str) -> _RunBinding:
        run_id = require_product_identifier(run_id, field="run_id")
        async with self._lock:
            binding = self._bindings.get(run_id)
        if binding is None:
            raise ProductStateError("product-execution-binding-missing", run_id)
        return binding

    async def _predecessor_reports(
        self, run_id: str, role: ProductRole
    ) -> tuple[tuple[ProductRole, str], ...]:
        if role is ProductRole.PARENT:
            return ()
        predecessors = (
            (ProductRole.PARENT,)
            if role is ProductRole.REVIEWER
            else (ProductRole.PARENT, ProductRole.REVIEWER)
        )
        binding = await self._binding(run_id)
        available = set(PRODUCT_MODE_ROLES[binding.assembly.resolved_mode])
        reports: list[tuple[ProductRole, str]] = []
        for predecessor in predecessors:
            if predecessor not in available:
                continue
            node_id = product_role_node_id(predecessor)
            agent_id, _, _, message_id = agent_identity(run_id, node_id)
            report = await self._supervisor.report(agent_id, message_id)
            if report.status != "completed" or type(report.final_text) is not str:
                raise ProductStateError("product-predecessor-report-invalid", run_id)
            reports.append(
                (predecessor, _bounded_text(report.final_text, self._max_report_chars))
            )
        return tuple(reports)


class ProductExecutionHost:
    """Own one process's ProductTask-to-Workflow execution receipts."""

    __slots__ = (
        "_closed",
        "_lock",
        "_owned",
        "_provisioner",
        "_resolver",
        "_workflow",
    )

    def __init__(
        self,
        workflow: WorkflowService,
        resolver: ProductWorkflowBindingResolver,
        provisioner: ProductTaskProvisioner,
    ) -> None:
        identity = durable_log_identity(workflow.store)
        if (
            durable_log_identity(resolver.store) is not identity
            or durable_log_identity(provisioner.store) is not identity
        ):
            raise ProductInputError("product-store-mismatch", "execution")
        self._workflow = workflow
        self._resolver = resolver
        self._provisioner = provisioner
        self._owned: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def store(self) -> EventStore:
        return self._workflow.store

    def owns_task(self, task_id: str) -> bool:
        try:
            task_id = require_product_identifier(task_id, field="task_id")
        except Exception:
            return False
        return task_id in self._owned

    async def prepare(self, task_id: str, preflight: ProductPreflight) -> None:
        task_id = require_product_identifier(task_id, field="task_id")
        async with self._lock:
            self._require_open()
            owner = await self._provisioner.prepare(task_id, preflight)
            owner = require_product_identifier(owner, field="owner_agent_id")
            current = self._owned.get(task_id)
            if current is not None and current != owner:
                raise ProductStateError("product-task-owner-conflict", task_id)
            self._owned[task_id] = owner

    async def start(
        self, task_id: str, assembly: ProductAssembly, *, requirement: str
    ) -> WorkflowRun:
        task_id = require_product_identifier(task_id, field="task_id")
        if type(assembly) is not ProductAssembly:
            raise ProductInputError("product-assembly-invalid", "assembly")
        owner = await self._owner(task_id)
        await self._resolver.bind(
            assembly,
            task_id=task_id,
            owner_agent_id=owner,
            requirement=requirement,
        )
        return await self._workflow.start(task_id, assembly.definition)

    async def resume(
        self, task_id: str, assembly: ProductAssembly
    ) -> WorkflowRun:
        task_id = require_product_identifier(task_id, field="task_id")
        if type(assembly) is not ProductAssembly:
            raise ProductInputError("product-assembly-invalid", "assembly")
        owner = await self._owner(task_id)
        await self._resolver.bind(
            assembly,
            task_id=task_id,
            owner_agent_id=owner,
            requirement=None,
        )
        return await self._workflow.resume(task_id, assembly.definition)

    async def state(
        self, task_id: str, assembly: ProductAssembly
    ) -> WorkflowRun:
        task_id = require_product_identifier(task_id, field="task_id")
        if type(assembly) is not ProductAssembly:
            raise ProductInputError("product-assembly-invalid", "assembly")
        return await self._workflow.state(task_id, assembly.definition)

    async def cancel(self, task_id: str) -> None:
        task_id = require_product_identifier(task_id, field="task_id")
        workflow_cancel = asyncio.create_task(
            self._workflow.cancel(task_id),
            name=f"traceh-product-workflow-cancel-{task_id}",
        )
        failures: list[BaseException] = []
        try:
            await self._provisioner.interrupt(task_id)
        except BaseException as error:
            failures.append(error)
        try:
            await asyncio.shield(workflow_cancel)
        except BaseException as error:
            failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Product execution cancellation failed", failures)

    async def release(self, task_id: str, *, reason: str) -> None:
        task_id = require_product_identifier(task_id, field="task_id")
        await self._provisioner.release(task_id, reason=reason)
        await self._resolver.unbind(task_id)
        async with self._lock:
            self._owned.pop(task_id, None)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            self._owned.clear()
        failures: list[BaseException] = []
        try:
            await self._workflow.aclose()
        except BaseException as error:
            failures.append(error)
        try:
            await self._provisioner.aclose()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise BaseExceptionGroup("Product execution close failed", failures)

    async def _owner(self, task_id: str) -> str:
        async with self._lock:
            self._require_open()
            owner = self._owned.get(task_id)
        if owner is None:
            raise ProductStateError("product-task-not-owned", task_id)
        return owner

    def _require_open(self) -> None:
        if self._closed:
            raise ProductStateError("product-execution-closed")


def product_task_owner_id(task_id: str) -> str:
    """The non-model Agent identity anchoring one task's ownership/Budget tree."""

    from traceh.api.json_types import fingerprint

    task_id = require_product_identifier(task_id, field="task_id")
    return "product-owner-" + fingerprint(
        {"purpose": "product-task-owner", "task_id": task_id}
    )


def _role_for_binding(binding_id: str, *, kind: str) -> ProductRole:
    if type(binding_id) is not str:
        raise ProductInputError("product-binding-invalid", kind)
    function = product_spec_binding if kind == "spec" else product_message_binding
    for role in ProductRole:
        if binding_id == function(role):
            return role
    raise ProductInputError("product-binding-unknown", binding_id)


def _require_role_node(role: ProductRole, node_id: str, run_id: str) -> None:
    if type(node_id) is not str or node_id != product_role_node_id(role):
        raise ProductStateError("product-role-node-mismatch", run_id)


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n[report truncated by host]"
    room = max(0, limit - len(marker))
    return value[:room] + marker[: limit - room]


def _role_message(
    role: ProductRole,
    requirement: str,
    reports: tuple[tuple[ProductRole, str], ...],
) -> str:
    instructions = {
        ProductRole.PARENT: (
            "Analyze the requirement and return a concise implementation plan. "
            "Do not modify the workspace."
        ),
        ProductRole.REVIEWER: (
            "Review the proposed approach for correctness and missing risks. "
            "Do not modify the workspace."
        ),
        ProductRole.CODER: (
            "Implement the requirement in the managed workspace, then run the "
            "relevant checks and report the result."
        ),
    }
    parts = [instructions[role], "", "Requirement:", requirement]
    for report_role, report in reports:
        parts.extend(("", f"{report_role.value.title()} report:", report))
    return "\n".join(parts)


__all__ = [
    "ProductExecutionHost",
    "ProductTaskProvisioner",
    "ProductWorkflowBindingResolver",
    "product_task_owner_id",
]
