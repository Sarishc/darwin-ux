"""MuseAdapter: an explicit, unimplemented seam.

What is known (checked 2026-09-27): no Muse package is installed; TypeSafe
AI's official documentation (docs.typesafe.ai: models, API reference,
introduction, primitives) lists only Jev and its System One decision
endpoint — it does not mention Muse; OPEN_QUESTIONS.md B2 records that even
Muse's provider is unverified. There is therefore no documented interface
to implement, and none is invented here: no endpoint, SDK, model id,
credential or request/response field.

Constructing the adapter always raises MuseNotConfiguredError, so
GENERATOR=muse fails clearly and nothing falls back to another generator.
When a documented interface exists, implement `generate` here: translate a
MutationRequest into Muse's input and Muse's output into a MutationSpec
dict — which then goes through exactly the same validation as the fixture.
"""

from .port import GeneratorReply, GeneratorUnavailableError
from .request import MutationRequest

MUSE_VERSION = "muse:unimplemented"


class MuseNotConfiguredError(GeneratorUnavailableError):
    pass


class MuseAdapter:
    name = "muse"
    version = MUSE_VERSION

    def __init__(self) -> None:
        raise MuseNotConfiguredError(
            "Muse is not available: no documented Muse interface exists "
            "(OPEN_QUESTIONS.md B2). Use GENERATOR=fixture or GENERATOR=llm."
        )

    def generate(self, request: MutationRequest) -> GeneratorReply:  # pragma: no cover
        raise MuseNotConfiguredError("Muse is not available")
