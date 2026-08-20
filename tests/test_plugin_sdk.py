"""The external plugin surface must expose every D3 contribution protocol."""

from __future__ import annotations


def test_plugin_sdk_exports_execution_capability_protocols() -> None:
    from traceh.plugins import (
        CommandVerifier,
        CompletionVerifier,
        DecisionKind,
        ToolCall,
        ToolCallNext,
        ToolDecision,
        ToolInvocation,
        ToolMiddleware,
        ToolPolicy,
        VerificationResult,
    )

    assert CommandVerifier
    assert CompletionVerifier
    assert DecisionKind
    assert ToolCall
    assert ToolCallNext
    assert ToolDecision
    assert ToolInvocation
    assert ToolMiddleware
    assert ToolPolicy
    assert VerificationResult
