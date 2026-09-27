"""The MutationGenerator port, owned by DarwinUX.

The fixture, the LLM baseline and the Muse seam implement it.

A generator receives a validated MutationRequest and returns its output
UNVALIDATED (a mapping or JSON text) plus its exact version. It gets no
database, file system, tool, repository or network capability from DarwinUX.
Failures are the three errors below; each ends the run without a candidate.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .request import MutationRequest


@dataclass(frozen=True)
class GeneratorReply:
    output: Mapping[str, Any] | str
    generator_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class GeneratorUnavailableError(RuntimeError):
    pass


class GeneratorFailureError(RuntimeError):
    pass


class GeneratorTimeoutError(GeneratorFailureError):
    pass


class MutationGenerator(Protocol):
    @property
    def name(self) -> str: ...  # "fixture" | "llm" | "muse"

    @property
    def version(self) -> str: ...

    def generate(self, request: MutationRequest) -> GeneratorReply: ...
