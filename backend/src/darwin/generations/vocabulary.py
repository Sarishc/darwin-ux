"""The closed vocabulary of promotion and rollback. The database CHECKs repeat it."""

from typing import Literal, get_args

POLICY_VERSION = "promotion_policy.v1"

Decision = Literal["approve", "reject"]
DECISIONS: tuple[Decision, ...] = get_args(Decision)

ChangeKind = Literal["bootstrap", "promotion", "rollback"]
CHANGE_KINDS: tuple[ChangeKind, ...] = get_args(ChangeKind)

# Self-asserted operator name (NOT authenticated identity): short, no spaces, no '@'.
REVIEWER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
REASON_MAX = 500
SPEC_HASH_PATTERN = r"^[0-9a-f]{64}$"
