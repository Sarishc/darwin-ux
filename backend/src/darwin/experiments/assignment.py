"""Deterministic traffic assignment: the ONE place a session gets a variant.

    bucket  = int(sha256(f"{experiment_key}:{session_id}")[:8 bytes], big-endian) % 10 000
    variant = "candidate" if bucket < candidate_allocation_bp else "control"

- Stable across processes, machines and restarts: SHA-256 of a canonical
  string (the session UUID in lowercase hyphenated form). Python's built-in
  hash() is salted per process and is never used; neither is any RNG.
- Salted by experiment key, so two experiments bucket the same session
  independently.
- Exact: allocation is an integer number of basis points (1 bp = 1 bucket =
  0.01%); there is no floating-point comparison anywhere. The 64-bit value's
  modulo bias over 10 000 buckets is below 1e-15, i.e. negligible.
- Pure: no database, no clock. The backend serves the result; the browser
  never computes its own assignment.
"""

import hashlib
import re
import uuid

from .vocabulary import (
    CANDIDATE_ALLOCATIONS_BP,
    EXPERIMENT_KEY_PATTERN,
    TOTAL_BUCKETS,
    Variant,
)

_KEY = re.compile(EXPERIMENT_KEY_PATTERN)


class AllocationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_allocation(candidate_bp: object) -> int:
    """An allowlisted integer (1/5/10/25/50%), or AllocationError. bool/float/str are refused."""
    if type(candidate_bp) is not int:  # not isinstance: True is an int, 10.0 is not allowed
        raise AllocationError("allocation_not_integer")
    if candidate_bp not in CANDIDATE_ALLOCATIONS_BP:
        raise AllocationError("allocation_not_allowlisted")
    return candidate_bp


def validate_key(experiment_key: object) -> str:
    if not isinstance(experiment_key, str) or not _KEY.fullmatch(experiment_key):
        raise AllocationError("experiment_key_invalid")
    return experiment_key


def bucket(experiment_key: str, session_id: uuid.UUID) -> int:
    validate_key(experiment_key)
    if not isinstance(session_id, uuid.UUID):
        raise AllocationError("session_id_invalid")
    digest = hashlib.sha256(f"{experiment_key}:{session_id}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % TOTAL_BUCKETS


def assign(experiment_key: str, session_id: uuid.UUID, candidate_bp: int) -> Variant:
    """The variant for this session in this experiment. Raises on any invalid input."""
    allocation = validate_allocation(candidate_bp)
    return "candidate" if bucket(experiment_key, session_id) < allocation else "control"
