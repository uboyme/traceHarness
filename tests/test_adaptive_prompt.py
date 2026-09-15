from dataclasses import replace

import pytest
from test_product_f3_e2e import _profile

from traceh.api.product import ProductRole, RequestedTaskMode
from traceh.product.runtime import (
    CODING_PROMPT_IDS,
    INVESTIGATION_PROMPT_IDS,
    MULTI_PROMPT_ID,
    MULTI_PROMPT_IDS,
    BuiltinProductAssemblyResolver,
)
from traceh.supervision.delegation import INVESTIGATION_TOOL_IDS


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["review-main", "diagnostic-main"])
async def test_adaptive_prompt_is_bound_to_resolved_capabilities_not_task_names(preset):
    profile = _profile(RequestedTaskMode.MULTI)
    resolver = BuiltinProductAssemblyResolver()
    coder = replace(profile.coder, preset=preset)
    single = await resolver.role_assembly(
        role=ProductRole.CODER, profile=coder, provider_id="provider", model_id="model"
    )
    multi = await resolver.role_assembly(
        role=ProductRole.CODER,
        profile=replace(
            coder, capability_grants=(*coder.capability_grants, *INVESTIGATION_TOOL_IDS)
        ),
        provider_id="provider",
        model_id="model",
    )
    child = await resolver.role_assembly(
        role=ProductRole.INVESTIGATOR,
        profile=profile.investigator,
        provider_id="provider",
        model_id="model",
    )
    assert single.prompt_ids == CODING_PROMPT_IDS
    assert multi.prompt_ids == MULTI_PROMPT_IDS
    assert child.prompt_ids == INVESTIGATION_PROMPT_IDS
    assert MULTI_PROMPT_ID not in single.prompt_ids + child.prompt_ids
    assert set(multi.tool_ids) - set(single.tool_ids) == set(INVESTIGATION_TOOL_IDS)
