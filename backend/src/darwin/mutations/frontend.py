"""Validate candidate UI Specs with the frontend's REAL Zod schema (cross-language contract).

Writes candidates to a temporary directory OUTSIDE the repository, runs
`npm run --silent validate-spec -- <files>` in frontend/ (the exact schema the
demo renders with), and returns one verdict per candidate. Used by the
integration tests, the evaluation and the CLI — never by the request path,
which must not depend on Node at runtime.
"""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darwin.memory.corpus import REPO_ROOT

FRONTEND_DIR = REPO_ROOT / "frontend"


class FrontendValidatorUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrontendVerdict:
    ok: bool
    issues: tuple[str, ...]


def validate_with_frontend(specs: Sequence[dict[str, Any]]) -> list[FrontendVerdict]:
    if not specs:
        return []
    npm = shutil.which("npm")
    if npm is None or not (FRONTEND_DIR / "node_modules").is_dir():
        raise FrontendValidatorUnavailableError("npm or frontend/node_modules not available")
    with tempfile.TemporaryDirectory(prefix="darwin-candidates-") as tmp:
        paths = []
        for i, spec in enumerate(specs):
            path = Path(tmp) / f"candidate-{i}.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            paths.append(str(path))
        completed = subprocess.run(  # noqa: S603 — fixed command, temp-file arguments only
            [npm, "run", "--silent", "validate-spec", "--", *paths],
            cwd=FRONTEND_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    if len(lines) != len(specs):
        raise FrontendValidatorUnavailableError("validate-spec returned an unexpected result")
    return [FrontendVerdict(bool(r["ok"]), tuple(r["issues"])) for r in lines]


def frontend_json_schema() -> dict[str, Any]:
    npm = shutil.which("npm")
    if npm is None:
        raise FrontendValidatorUnavailableError("npm not available")
    completed = subprocess.run(  # noqa: S603
        [npm, "run", "--silent", "validate-spec", "--", "--json-schema"],
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return dict(json.loads(completed.stdout))
