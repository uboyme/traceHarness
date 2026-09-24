"""Connect clustered findings to one original AO proposal; never evaluate (ADR-0082)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from traceh.evaluation.model_evidence import load_model_call
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.background import EpisodeReservation, EpisodeSettlement
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationLimits,
    editable_text,
)
from traceh.evolution.strategy import inspect_strategy_optimization, propose_strategy


class BackgroundProposal:
    def __init__(
        self,
        template,
        *,
        provider,
        analysis_config,
        output,
        selectors,
        timeout_seconds,
        max_request_bytes,
    ):
        self.template, self.provider = template, provider
        self.analysis_config = analysis_config
        self.output = Path(output).resolve()
        self.selectors, self.timeout_seconds = selectors, timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.files = source_files()[1]
        self.baseline = source_digest(self.files)
        self.editable = editable_text(self.files, selectors)
        self.case_ids = tuple(dict.fromkeys(t.case_id for t in template.trials))
        self.reservation = EpisodeReservation(analysis_config.token_limit)

    async def __call__(self, episode_id, observations, seen, deadline):
        if source_digest(source_files()[1]) != self.baseline:
            raise ValueError("background-source-changed")
        limits = OptimizationLimits(
            1,
            1,
            len(self.template.trials),
            1,
            1,
            1,
            1,
            self.analysis_config.token_limit,
            self.max_request_bytes,
        )
        contract = OptimizationContract(
            episode_id,
            self.baseline,
            self.template.manifest.document.sha256,
            self.template.options.document.sha256,
            self.template.manifest.dataset.sha256,
            self.case_ids,
            self.editable,
            limits,
            min(deadline, datetime.now(UTC) + timedelta(seconds=self.timeout_seconds)),
        )
        root = self.output / episode_id
        await propose_strategy(
            self.template,
            contract,
            observations=observations,
            provider=self.provider,
            analysis_config=self.analysis_config,
            output_dir=root,
            seen_candidate_digests=seen,
        )
        return inspect_background_proposal(root)


def inspect_background_proposal(root):
    """The original inspector validates the call and patch; this derives scheduling status."""
    root = Path(root).resolve()
    result = inspect_strategy_optimization(root)
    if not (root / "analysis").exists():
        # Refused before any model call was made: nothing was spent.
        known, converged = True, True
    elif (root / "analysis/result.json").exists():
        receipt = load_model_call(root / "analysis")[1]
        usage = result["cost"]["analysis"]
        converged = receipt["converged"]
        known = usage is not None and usage["total_tokens"] is not None
    else:
        known, converged = False, False
    candidate = result["candidate"]
    return EpisodeSettlement(
        str(root), None if candidate is None else candidate["digest"], known, converged
    )
