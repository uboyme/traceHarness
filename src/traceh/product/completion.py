"""Writable main completion uses the frozen host checks and original receipts."""

from traceh.api.json_types import canonical_json
from traceh.api.sandbox import SandboxOwner
from traceh.product.verification_review import WorkspaceDeliveryReader
from traceh.promotion.models import (
    freeze_verification_plan,
    verification_evidence_digest,
    verifier_command_digest,
    verifier_definition_digest,
    verifier_result_data,
)
from traceh.runtime.verification import VerificationResult
from traceh.session.service import SessionService
from traceh.supervision.execution import durable_log_identity


class WritableCompletionVerifier:
    def __init__(self, integration, plan, runner, *, agent_id, session_id):
        self.integration = integration
        self.plan = freeze_verification_plan(plan)
        self.runner = runner
        self.agent_id = agent_id
        self.session_id = session_id
        self.sessions = SessionService(integration.writable.supervisor.store)
        if runner.sandbox_service is not None and durable_log_identity(
            runner.sandbox_service.store
        ) is not durable_log_identity(self.sessions.store):
            raise ValueError("product-completion-store-mismatch")

    async def verify(self, workspace):
        events = await self.sessions.read_session(self.session_id)
        current = next(e for e in reversed(events) if e.type == "step/start").data
        integration = await self.integration.completion_receipt(
            session_id=self.session_id, turn_id=current["turn_id"], workspace=workspace,
        )
        workspaces = self.integration.writable.workspaces
        delivery = WorkspaceDeliveryReader(
            workspaces, self.integration.limits, self.agent_id, self.session_id,
        )
        before = await delivery.read()
        # The original workspace lock excludes edits/release while the sandbox snapshots
        # and checks this candidate. The sandbox never publishes verifier changes back.
        async with workspaces.inspect_session(self.session_id, agent_id=self.agent_id) as (
            handle, _,
        ):
            if handle.root != workspace:
                raise ValueError("product-completion-workspace-mismatch")
            evidence = await self.runner.run(
                self.plan,
                cwd=workspace,
                owner=SandboxOwner(
                    "verification", current["step_id"], self.session_id,
                    current["turn_id"], current["step_id"], agent_id=self.agent_id,
                ),
                stream_id=self.sessions.session_stream(self.session_id),
            )
        after = await delivery.read()
        stable = before == after
        # Public requirement text may only label results from its exact frozen check.
        if (
            evidence.definition_digest != verifier_definition_digest(self.plan)
            or len(evidence.results) != len(self.plan.commands)
            or any(
                result.command_id != command.command_id
                or result.argv_digest != verifier_command_digest(command)
                for command, result in zip(self.plan.commands, evidence.results, strict=True)
            )
            or evidence.evidence_digest != verification_evidence_digest(
                evidence.definition_digest, evidence.results
            )
            or evidence.passed is not all(result.passed for result in evidence.results)
        ):
            raise ValueError("product-completion-verification-binding-invalid")
        return VerificationResult(
            bool(integration) and evidence.passed and stable,
            canonical_json(dict(
                kind="writable-completion", integration=integration,
                integration_check=(
                    f"{len(integration)} applied receipt(s) match every accepted writable "
                    "assignment and this target."
                    if integration else
                    "A required assistant Patch integration is missing. Read each exact "
                    "assistant Patch and use integrate_child_patch for every accepted "
                    "writable assignment; copying code or claiming integration does not "
                    "supply an applied receipt."
                ),
                delivery=after, candidate_unchanged=stable,
                verifier_definition_digest=evidence.definition_digest,
                verification_evidence_digest=evidence.evidence_digest,
                results=[dict(
                    **verifier_result_data(result),
                    public_requirement=command.public_requirement,
                ) for command, result in zip(self.plan.commands, evidence.results, strict=True)],
                diagnostic_scope="command-result-only",
                failure_cause="unknown",
                raw_output_availability="not-retained",
                interpretation=(
                    "Fixed host checks on the current workspace snapshot. Only declared checks "
                    "are covered; this is not an Artifact Review or human approval. A public "
                    "requirement describes the host-declared check, not the exact failing "
                    "assertion or root cause. Null means no public requirement was declared. "
                    "A failed command does not prove which internal condition failed; a "
                    "start failure, timeout or output limit does not judge implementation "
                    "correctness. Raw verifier output is not retained and cannot be found "
                    "with retained-output tools. Execution references identify receipts, not "
                    "readable stderr. Use the public requirements, available source and your "
                    "own permitted checks to diagnose failures within the existing bound. "
                    "If the candidate changed, these results do not establish its current state."
                ),
            )),
        )
