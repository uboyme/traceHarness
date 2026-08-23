"""Host policy seam for turning child intent into an Agent proposal.

The model-visible ``spawn_agent`` Tool supplies only an existing preset and
workspace *intent*.  A host policy must explicitly convert that intent into a
small proposal before the existing Supervisor creates an ``AgentSpec``.  The
policy cannot select a concrete Provider, model, prompt, runtime or task: the
``AgentActivationFactory`` continues to resolve runtime capabilities from the
approved preset, and work is sent separately through ``send_agent_message``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from traceh.api.agents import AgentRecord
from traceh.api.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class ChildProvisioningProposal:
    """The deliberately small result of one host provisioning decision."""

    preset: str
    workspace_id: str
    metadata: dict[str, JsonValue] = field(default_factory=dict)


class ChildProvisioningPolicy(Protocol):
    """Host-owned, synchronous policy for one requested child.

    Implementations may approve, reject or map the requested identifiers using
    explicit host configuration.  For the same durable owner and requested
    intent they must return the same identity-defining preset/workspace;
    otherwise an idempotent Tool replay fails closed. Metadata is descriptive
    under the existing Agent identity protocol, so the first committed value
    wins rather than making metadata a second request identity.
    """

    def propose_child(
        self,
        *,
        owner: AgentRecord,
        requested_preset: str,
        requested_workspace_id: str,
    ) -> ChildProvisioningProposal:
        """Return the host-approved child proposal or raise to reject it."""


__all__ = ["ChildProvisioningPolicy", "ChildProvisioningProposal"]
