"""The one traced path to an LLM provider (Step 16).

    result = generate_structured(llm, request)    # instead of llm.generate_structured(request)

Records an `llm.generate` span and bounded metrics: provider, model, request version,
token counts as reported, latency and a status category. It NEVER records the
instructions, the evidence, the output schema or the output text. Behaviour is
unchanged: the provider's exceptions propagate exactly as before.
"""

import time

from darwin.observability import record, span

from .port import (
    LLMProvider,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)


def _status(error: BaseException | None) -> str:
    if error is None:
        return "ok"
    if isinstance(error, ProviderUnavailableError):
        return "unavailable"
    if isinstance(error, ProviderTimeoutError):
        return "timeout"
    return "error"


def generate_structured(
    llm: LLMProvider, request: StructuredGenerationRequest
) -> StructuredGenerationResult:
    provider = str(getattr(llm, "name", "unknown"))
    started = time.perf_counter()
    status = "error"
    with span(
        "llm.generate",
        {
            "gen_ai.system": provider,
            "gen_ai.request.model": str(getattr(llm, "model", "unknown")),
            "darwin.llm.request_version": request.request_version,
        },
    ) as s:
        try:
            result = llm.generate_structured(request)
            status = "ok"
        except BaseException as error:
            status = _status(error)
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            s.set(**{"darwin.status": status})
            labels = {"provider": provider, "status": status}
            record(
                "darwin.llm.calls",
                1,
                provider=provider,
                request_version=request.request_version,
                status=status,
            )
            record("darwin.llm.duration", elapsed, **labels)
        usage = result.usage
        if usage is not None:
            s.set(
                **{
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                }
            )
            for direction, tokens in (
                ("input", usage.input_tokens),
                ("output", usage.output_tokens),
            ):
                if tokens:
                    record("darwin.llm.tokens", tokens, provider=provider, direction=direction)
        return result
