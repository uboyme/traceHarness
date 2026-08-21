"""Development control-plane services for controlled capability evolution.

This package is deliberately outside :mod:`traceh.runtime`.  Candidate source,
builds, tests and evidence reports are development-time concerns; they are not
Agent state and must never become another plugin loader or Session fact source.
"""

from traceh.evolution.candidate_comparison import (
    CandidateComparator,
    CandidateComparisonConfig,
    CandidateComparisonReport,
)
from traceh.evolution.candidate_promotion import (
    CandidatePromoter,
    CandidatePromotionConfig,
    CandidatePromotionRollbackError,
    CandidateRollbackConfig,
    CandidateRollbacker,
    PromotionReport,
)
from traceh.evolution.candidate_validation import (
    CandidateValidationConfig,
    CandidateValidationReport,
    CandidateValidator,
)

__all__ = [
    "CandidateComparator",
    "CandidateComparisonConfig",
    "CandidateComparisonReport",
    "CandidateValidationConfig",
    "CandidateValidationReport",
    "CandidateValidator",
    "CandidatePromoter",
    "CandidatePromotionConfig",
    "CandidatePromotionRollbackError",
    "CandidateRollbackConfig",
    "CandidateRollbacker",
    "PromotionReport",
]
