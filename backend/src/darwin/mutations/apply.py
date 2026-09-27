"""Pure mutation application and the protected-field diff.

apply_mutation never touches its input, the database or the filesystem: it
deep-copies the source spec, replaces exactly the validated (component,
property) values, sets `generation` to the candidate's generation (a
DarwinUX-assigned field no generator can name) and returns the new object.

diff_paths is an independent, generic check: it walks source and candidate
side by side and reports every leaf that differs. The service accepts a
candidate only if the differing leaves are exactly the operations' targets
plus `generation` — so even a bug in the applier cannot slip another change
through.
"""

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .surface import index_targets

MAX_SPEC_BYTES = 64 * 1024


@dataclass(frozen=True)
class Change:
    component_id: str
    property: str
    value: Any


def canonical_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def apply_mutation(
    source: dict[str, Any], changes: Sequence[Change], candidate_generation: int
) -> dict[str, Any]:
    candidate = copy.deepcopy(source)
    targets = index_targets(candidate)
    for change in changes:
        targets[change.component_id].node[change.property] = change.value
    candidate["generation"] = candidate_generation
    return candidate


def diff_paths(source: Any, candidate: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every path where the two JSON trees differ (structure or leaf value)."""
    if isinstance(source, dict) and isinstance(candidate, dict):
        if source.keys() != candidate.keys():
            return [path]
        return [p for k in source for p in diff_paths(source[k], candidate[k], (*path, str(k)))]
    if isinstance(source, list) and isinstance(candidate, list):
        if len(source) != len(candidate):
            return [path]
        return [
            p
            for i, (a, b) in enumerate(zip(source, candidate, strict=True))
            for p in diff_paths(a, b, (*path, str(i)))
        ]
    if type(source) is not type(candidate) or source != candidate:
        return [path]
    return []


def expected_paths(source: dict[str, Any], changes: Sequence[Change]) -> set[tuple[str, ...]]:
    """The only paths a candidate may differ at: each change's leaf, plus `generation`."""
    locations = _locations(source)
    allowed: set[tuple[str, ...]] = {("generation",)}
    for change in changes:  # validation already refused no-op changes
        allowed.add((*locations[change.component_id], change.property))
    return allowed


def _locations(spec: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for si, section in enumerate(spec["page"]["sections"]):
        base = ("page", "sections", str(si))
        out[section["id"]] = base
        for ci, component in enumerate(section["components"]):
            cbase = (*base, "components", str(ci))
            out[component["id"]] = cbase
            if component["type"] == "plan_grid":
                for pi, plan in enumerate(component["plans"]):
                    pbase = (*cbase, "plans", str(pi))
                    out[plan["id"]] = pbase
                    out[plan["cta"]["id"]] = (*pbase, "cta")
    return out
