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


def test_plugin_sdk_exports_the_same_typed_skill_contract() -> None:
    import traceh.api as api
    import traceh.plugins as sdk

    for name in (
        "SkillChunk", "SkillContribution", "SkillDescriptor", "SkillLimits", "SkillPolicy",
        "SkillResource", "SkillResourceRoot", "SkillSection", "SkillSectionContent",
    ):
        assert getattr(sdk, name) is getattr(api, name)
    assert callable(sdk.PluginContext.register_skill)
