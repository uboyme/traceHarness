"""Connect runtime feedback to the existing one-proposal AO/Evaluation path."""

import asyncio
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
from traceh.evolution.strategy import inspect_strategy_optimization, run_strategy_optimization


class BackgroundExperiment:
    def __init__(
        self,
        template,
        *,
        provider,
        analysis_config,
        judge_config,
        output,
        selectors,
        timeout_seconds,
        max_request_bytes,
    ):
        self.template, self.provider = template, provider
        self.analysis_config, self.judge_config = analysis_config, judge_config
        self.output = Path(output).resolve()
        self.selectors, self.timeout_seconds = selectors, timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.files = source_files()[1]
        self.baseline = source_digest(self.files)
        self.editable = editable_text(self.files, selectors)
        self.reservation = EpisodeReservation(
            len(template.trials),
            analysis_config.token_limit + len(template.trials) * judge_config.token_limit,
        )

    async def __call__(self, episode_id, observations, seen, deadline):
        if source_digest(source_files()[1]) != self.baseline:
            raise ValueError("background-source-changed")
        root = self.output / episode_id
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
            tuple(dict.fromkeys(t.case_id for t in self.template.trials)),
            self.editable,
            limits,
            min(deadline, datetime.now(UTC) + timedelta(seconds=self.timeout_seconds)),
        )
        try:
            await run_strategy_optimization(
                self.template,
                contract,
                observations=observations,
                provider=self.provider,
                analysis_config=self.analysis_config,
                judge_provider=self.provider,
                judge_config=self.judge_config,
                output_dir=root,
                seen_candidate_digests=seen,
            )
        except asyncio.CancelledError:
            # The original owner has already converged. Inspect whatever it actually wrote;
            # missing evidence remains unknown and blocks another experiment.
            pass
        return inspect_background_experiment(root)


def inspect_background_experiment(root):
    """Original inspectors validate requests/grades; this only derives scheduling status."""
    root = Path(root).resolve()
    result = inspect_strategy_optimization(root)
    analysis = result["cost"]["analysis"]
    known = bool(analysis is not None and analysis["total_tokens"] is not None)
    converged = False
    if (root / "analysis/result.json").exists():
        converged = load_model_call(root / "analysis")[1]["converged"]
    known = known and result["cost"]["review_tokens"] is not None
    candidate = None
    if result["evaluation"]:
        evaluation = result["evaluation"]
        converged = converged and evaluation["progress"]["convergence"] == "converged"
        for row in evaluation["rounds"]:
            candidate = row.get("candidate_digest", candidate)
            comparison = row["comparison"]
            known = (
                known
                and comparison is not None
                and comparison["status"] != "not_comparable"
                and all(arm["cost"]["total_tokens"] is not None for arm in comparison["arms"])
            )
    # An admitted candidate requiring review stops collection from causing a new experiment.
    return EpisodeSettlement(
        str(root),
        candidate,
        result["action"] in {"await_review", "review_candidate"},
        known,
        converged,
    )
