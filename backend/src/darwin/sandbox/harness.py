"""Running the frontend sandbox harness and validating what it reports.

The backend writes {"specs": {key: spec}} to an OS temp directory, runs
`npm run --silent sandbox-harness` in frontend/ with the input/output paths
in the ENVIRONMENT (an argument array, never a shell string, never built from
spec content), reads the facts back and validates their shape strictly. The
temp directory is removed afterwards. No network is used.

Anything unexpected — npm missing, non-zero exit, timeout, missing or
malformed output, a wrong harness version — is a HarnessError, which the
evaluation turns into a fail-closed `human_review`, never `pass`.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from darwin.memory.corpus import REPO_ROOT

FRONTEND_DIR = REPO_ROOT / "frontend"
HARNESS_VERSION = "sandbox_harness.v1"
TIMEOUT_SECONDS = 180


class HarnessError(RuntimeError):
    """The harness could not produce trustworthy facts. `code` is machine-readable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _model() -> ConfigDict:
    return ConfigDict(extra="forbid", frozen=True)


class SchemaFacts(BaseModel):
    model_config = _model()
    ok: bool
    issues: list[str]


class RenderFacts(BaseModel):
    model_config = _model()
    ok: bool
    error: str | None
    dom_nodes: int = Field(ge=0)
    buttons: int = Field(ge=0)


class AxeViolation(BaseModel):
    model_config = _model()
    id: str
    impact: str
    nodes: int = Field(ge=0)


class AccessibilityFacts(BaseModel):
    model_config = _model()
    initial: list[AxeViolation]
    revealed: list[AxeViolation]
    disabled_rules: list[str]


class SemanticsFacts(BaseModel):
    model_config = _model()
    heading_levels: list[int]
    inputs: int = Field(ge=0)
    labelled_inputs: int = Field(ge=0)
    focusable_buttons: int = Field(ge=0)


class CtaFacts(BaseModel):
    model_config = _model()
    component_id: str
    telemetry_components: list[str]
    reveal_delay_ms: int | None
    signup_hidden_before_click: bool


class FormError(BaseModel):
    model_config = _model()
    field: str
    reason: str


class FormFacts(BaseModel):
    model_config = _model()
    present: bool
    submit_empty_errors: list[FormError]
    summary_alert_shown: bool
    per_field_errors_shown: int = Field(ge=0)
    inline_error_on_blur: bool
    completed_with_valid_values: bool
    telemetry_components: list[str]


class TelemetryFacts(BaseModel):
    model_config = _model()
    payloads: list[dict[str, str | int | float]]
    values_leaked: bool


class SpecFacts(BaseModel):
    model_config = _model()
    schema_: SchemaFacts = Field(alias="schema")
    render: RenderFacts
    accessibility: AccessibilityFacts
    semantics: SemanticsFacts
    ctas: list[CtaFacts]
    form: FormFacts | None
    telemetry: TelemetryFacts
    render_ms: float  # informational local jsdom timing; never gated


class HarnessOutput(BaseModel):
    model_config = _model()
    harness_version: str
    facts: dict[str, SpecFacts]


class HarnessRunner(Protocol):
    def run(self, specs: Mapping[str, dict[str, Any]]) -> dict[str, SpecFacts]: ...


def parse_output(raw: object, expected_keys: set[str]) -> dict[str, SpecFacts]:
    try:
        output = HarnessOutput.model_validate(raw)
    except ValidationError as error:
        raise HarnessError("harness_output_malformed") from error
    if output.harness_version != HARNESS_VERSION:
        raise HarnessError("harness_version_mismatch")
    if set(output.facts) != expected_keys:
        raise HarnessError("harness_output_incomplete")
    return output.facts


class NodeHarnessRunner:
    """The real harness: the frontend's own schema, registry and SpecPage under jsdom."""

    def run(self, specs: Mapping[str, dict[str, Any]]) -> dict[str, SpecFacts]:
        npm = shutil.which("npm")
        if npm is None or not (FRONTEND_DIR / "node_modules").is_dir():
            raise HarnessError("harness_unavailable")
        with tempfile.TemporaryDirectory(prefix="darwin-sandbox-") as tmp:
            source = Path(tmp) / "input.json"
            target = Path(tmp) / "output.json"
            source.write_text(json.dumps({"specs": dict(specs)}), encoding="utf-8")
            env = {**os.environ, "SANDBOX_INPUT": str(source), "SANDBOX_OUTPUT": str(target)}
            try:
                completed = subprocess.run(  # noqa: S603 — fixed argv; paths via env only
                    [npm, "run", "--silent", "sandbox-harness"],
                    cwd=FRONTEND_DIR,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise HarnessError("harness_timeout") from error
            if completed.returncode != 0:
                raise HarnessError("harness_failed")
            if not target.is_file():
                raise HarnessError("harness_output_missing")
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except ValueError as error:
                raise HarnessError("harness_output_malformed") from error
        return parse_output(raw, set(specs))
