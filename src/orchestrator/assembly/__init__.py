"""Metadata-first rough-cut assembly helpers."""

from src.orchestrator.assembly.candidate_matrix import (
    CandidateMatrixBuilder,
    build_slots_from_beat_map,
)
from src.orchestrator.assembly.export import (
    proposal_to_director_notes,
    proposal_to_semantic_edit,
    write_proposal_director_notes,
)
from src.orchestrator.assembly.feedback import compare_proposal_to_snapshot, run_resolve_feedback
from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.assembly.review import (
    ProposalReviewReport,
    ProposalReviewSuggestion,
    build_review_prompt,
    gemini_review_proposal,
    review_proposal,
    summarize_proposal_for_review,
)
from src.orchestrator.assembly.scoring import assemble_slots, score_candidate

__all__ = [
    "AssemblyCandidate",
    "AssemblyProposal",
    "AssemblySlot",
    "CandidateMatrixBuilder",
    "ProposalReviewReport",
    "ProposalReviewSuggestion",
    "assemble_slots",
    "build_slots_from_beat_map",
    "build_review_prompt",
    "compare_proposal_to_snapshot",
    "gemini_review_proposal",
    "proposal_to_director_notes",
    "proposal_to_semantic_edit",
    "review_proposal",
    "run_resolve_feedback",
    "score_candidate",
    "summarize_proposal_for_review",
    "write_proposal_director_notes",
]
