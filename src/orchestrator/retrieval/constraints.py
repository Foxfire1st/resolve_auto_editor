"""Explicit visual constraint extraction and candidate validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from src.gateway.profiles import default_profile_name, load_profile_manifest
from src.orchestrator.clip_chooser import _classify_visual_mode, _matches_any_constraint
from src.orchestrator.clip_slices import ClipSlice

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
_QUOTED_PATTERN = re.compile(r"['\"]([^'\"]{2,})['\"]")
_PAREN_PATTERN = re.compile(r"\(([^()]{2,})\)")
_POSSESSIVE_PATTERN = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:\s*&\s*[A-Z][A-Za-z0-9]*)?(?:\s+[A-Z][A-Za-z0-9]*)*)'s\b")
_PROPER_PHRASE_PATTERN = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:\s*(?:&|and)\s*[A-Z][A-Za-z0-9]+|(?:\s+[A-Z][A-Za-z0-9]+){0,4})"
)
_CAMEL_OR_CODE_PATTERN = re.compile(r"\b[A-Z]?[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[A-Z][0-9]\b|\b[A-Za-z]+\s+[0-9]\b")

_GENERIC_PROPER_STOPWORDS = {
    "And",
    "As",
    "But",
    "Display",
    "Focus",
    "Keywords",
    "Present",
    "Return",
    "Show",
    "The",
    "Then",
    "This",
    "Use",
    "With",
}
_ATTRIBUTE_TERMS = {
    "axe",
    "battle axe",
    "character model",
    "eidolon",
    "gun",
    "gunblade",
    "idle",
    "light cone",
    "menu",
    "model",
    "polearm",
    "relic",
    "relics",
    "trace",
    "traces",
    "weapon",
    "weapons",
}
_SOFT_INTENT_TERMS = {
    "fast-paced",
    "hype",
    "pensive",
    "transition",
}


@dataclass(frozen=True)
class ShotConstraints:
    """Hard and soft constraints extracted from a shot request."""

    required_subjects: tuple[str, ...] = ()
    required_targets: tuple[str, ...] = ()
    required_attributes: tuple[str, ...] = ()
    required_modes: tuple[str, ...] = ()
    soft_intent: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    profile_name: str | None = None
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def has_hard_constraints(self) -> bool:
        return bool(
            self.required_subjects
            or self.required_targets
            or self.required_attributes
            or self.required_modes
        )

    @property
    def has_exact_named_target(self) -> bool:
        return bool(self.required_targets)

    def to_debug(self) -> dict[str, Any]:
        return {
            "required_subjects": list(self.required_subjects),
            "required_targets": list(self.required_targets),
            "required_attributes": list(self.required_attributes),
            "required_modes": list(self.required_modes),
            "soft_intent": list(self.soft_intent),
            "search_terms": list(self.search_terms),
            "profile_name": self.profile_name,
        }


@dataclass(frozen=True)
class CandidateValidation:
    accepted: bool
    reasons: tuple[str, ...] = ()


def extract_shot_constraints(
    *,
    visual_goal: str,
    coverage_note: str = "",
    beat_intent: str = "",
    vo_excerpt: str = "",
    required_events: Sequence[str] = (),
    keywords: Sequence[str] = (),
    profile_name: str | None = None,
) -> ShotConstraints:
    """Extract explicit constraints without baking profile vocabulary into generic code."""

    effective_profile = profile_name or _safe_default_profile()
    aliases = _load_keyword_aliases(effective_profile)
    hard_text = " | ".join(
        text
        for text in [
            visual_goal,
            " | ".join(required_events),
            " | ".join(keywords),
        ]
        if text
    )
    context_text = " | ".join(text for text in [hard_text, coverage_note, beat_intent, vo_excerpt] if text)

    quoted = _unique(_clean_phrase(match) for match in _QUOTED_PATTERN.findall(hard_text))
    parenthetical = _unique(_clean_phrase(match) for match in _PAREN_PATTERN.findall(hard_text))
    keyword_phrases = _unique(_split_keywords(keywords))

    subjects = _unique(
        phrase
        for phrase in _iter_possessive_subjects(hard_text)
        if _looks_like_subject(phrase)
    )
    target_parenthetical = [phrase for phrase in parenthetical if "," not in phrase]
    targets = _unique(
        phrase
        for phrase in [*quoted, *target_parenthetical, *_iter_strong_keyword_targets(keyword_phrases), *_iter_code_terms(hard_text)]
        if _looks_like_named_target(phrase)
    )
    attributes = _unique(_extract_attributes(hard_text, aliases))
    modes = _extract_required_modes(visual_goal or coverage_note)
    soft = tuple(term for term in _SOFT_INTENT_TERMS if term in context_text.lower())

    search_terms = _unique(
        phrase
        for phrase in [*targets, *subjects, *attributes, *quoted, *parenthetical, *keyword_phrases]
        if _has_search_value(phrase)
    )
    expanded_terms = _unique([*search_terms, *_expand_aliases(search_terms, aliases)])

    return ShotConstraints(
        required_subjects=tuple(subjects),
        required_targets=tuple(targets),
        required_attributes=tuple(attributes),
        required_modes=tuple(modes),
        soft_intent=soft,
        search_terms=tuple(expanded_terms),
        profile_name=effective_profile,
        aliases=aliases,
    )


def filter_candidates_by_constraints(
    candidates: Sequence[ClipSlice],
    constraints: ShotConstraints,
) -> tuple[list[ClipSlice], list[dict[str, Any]]]:
    """Return candidates that satisfy hard constraints plus rejection details."""

    if not constraints.has_hard_constraints:
        return list(candidates), []

    accepted: list[ClipSlice] = []
    validation_log: list[dict[str, Any]] = []
    for candidate in candidates:
        validation = validate_candidate(candidate, constraints)
        validation_log.append(
            {
                "asset_id": candidate.asset_id,
                "accepted": validation.accepted,
                "reasons": list(validation.reasons),
            }
        )
        if validation.accepted:
            accepted.append(candidate)
    return accepted, validation_log


def validate_candidate(candidate: ClipSlice, constraints: ShotConstraints) -> CandidateValidation:
    text = _candidate_text(candidate)
    reasons: list[str] = []

    if constraints.required_subjects and not _matches_terms(constraints.required_subjects, text, constraints.aliases):
        reasons.append("missing required subject")
    if constraints.required_targets and not _matches_terms(constraints.required_targets, text, constraints.aliases):
        reasons.append("missing required named target")
    if constraints.required_attributes and not _matches_terms(constraints.required_attributes, text, constraints.aliases):
        reasons.append("missing required visual attribute")
    if constraints.required_modes:
        mode = _classify_visual_mode(text)
        if mode != "neutral" and mode not in constraints.required_modes:
            reasons.append(f"wrong visual mode ({mode})")

    return CandidateValidation(accepted=not reasons, reasons=tuple(reasons))


def _candidate_text(candidate: ClipSlice) -> str:
    return " ".join(
        filter(
            None,
            [
                candidate.asset_id,
                candidate.event_summary,
                candidate.description or "",
                " ".join(candidate.event_tags),
            ],
        )
    ).lower()


def _matches_terms(terms: Sequence[str], text: str, aliases: Mapping[str, Sequence[str]]) -> bool:
    expanded = _unique([*terms, *_expand_aliases(terms, aliases)])
    return _matches_any_constraint(expanded, text.lower())


def _load_keyword_aliases(profile_name: str | None) -> dict[str, tuple[str, ...]]:
    if not profile_name:
        return {}
    try:
        manifest = load_profile_manifest(profile_name)
    except (FileNotFoundError, ValueError):
        return {}
    raw = manifest.get("keyword_aliases") if isinstance(manifest, dict) else None
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, tuple[str, ...]] = {}
    for key, values in raw.items():
        if not isinstance(values, list):
            continue
        normalized_key = _clean_phrase(str(key)).lower()
        normalized_values = tuple(_clean_phrase(str(value)) for value in values if _clean_phrase(str(value)))
        if normalized_key and normalized_values:
            aliases[normalized_key] = normalized_values
    return aliases


def _safe_default_profile() -> str | None:
    try:
        return default_profile_name()
    except Exception:
        return None


def _expand_aliases(terms: Iterable[str], aliases: Mapping[str, Sequence[str]]) -> list[str]:
    expanded: list[str] = []
    for term in terms:
        normalized = term.lower().strip()
        expanded.extend(aliases.get(normalized, ()))
        for key, values in aliases.items():
            if key in normalized:
                expanded.extend(values)
    return expanded


def _extract_required_modes(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    if any(term in lowered for term in ("menu", "screen", "trace", "traces", "light cone", "relic", "eidolon", "details")):
        return ("ui",)
    if any(term in lowered for term in ("idle", "model", "character model", "weapon", "gunblade", "polearm")):
        return ()
    mode = _classify_visual_mode(text)
    return ("combat",) if mode == "combat" else ()


def _extract_attributes(text: str, aliases: Mapping[str, Sequence[str]]) -> list[str]:
    lowered = text.lower()
    attributes = [term for term in _ATTRIBUTE_TERMS if term in lowered]
    attributes.extend(_expand_aliases(attributes, aliases))
    return _unique(attributes)


def _split_keywords(keywords: Sequence[str]) -> list[str]:
    terms: list[str] = []
    for keyword in keywords:
        terms.extend(piece.strip(" .,:;\t\r\n") for piece in str(keyword).split(","))
    return [term for term in terms if term]


def _iter_strong_keyword_targets(keywords: Sequence[str]) -> list[str]:
    targets: list[str] = []
    for keyword in keywords:
        lowered = keyword.lower()
        if any(char.isdigit() for char in keyword):
            targets.append(keyword)
            continue
        if "menu" in lowered and any(token[:1].isupper() for token in _TOKEN_PATTERN.findall(keyword)):
            targets.append(keyword)
    return targets


def _iter_possessive_subjects(text: str) -> list[str]:
    subjects: list[str] = []
    for match in _POSSESSIVE_PATTERN.findall(text):
        phrase = _clean_phrase(match)
        for stopword in _GENERIC_PROPER_STOPWORDS:
            prefix = f"{stopword} "
            if phrase.startswith(prefix):
                phrase = phrase[len(prefix) :]
                break
        subjects.append(phrase)
    return subjects


def _iter_proper_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for match in _PROPER_PHRASE_PATTERN.findall(text):
        phrase = _clean_phrase(match)
        if not phrase or phrase in _GENERIC_PROPER_STOPWORDS:
            continue
        phrases.append(phrase)
    return phrases


def _iter_code_terms(text: str) -> list[str]:
    return [_clean_phrase(match) for match in _CAMEL_OR_CODE_PATTERN.findall(text)]


def _looks_like_subject(phrase: str) -> bool:
    if not phrase or phrase in _GENERIC_PROPER_STOPWORDS:
        return False
    tokens = _TOKEN_PATTERN.findall(phrase)
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0].lower() in _ATTRIBUTE_TERMS:
        return False
    return True


def _looks_like_named_target(phrase: str) -> bool:
    if not phrase or phrase in _GENERIC_PROPER_STOPWORDS:
        return False
    lowered = phrase.lower()
    if lowered in {"character", "model", "menu", "screen"}:
        return False
    return (
        len(_TOKEN_PATTERN.findall(phrase)) >= 2
        or phrase[:1].isupper()
        or any(char.isdigit() for char in phrase)
    )


def _has_search_value(phrase: str) -> bool:
    return any(len(token.strip("'")) >= 3 for token in _TOKEN_PATTERN.findall(phrase))


def _clean_phrase(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" .,:;\t\r\n")
    cleaned = cleaned.replace("’", "'")
    return cleaned


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = _clean_phrase(str(value))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result
