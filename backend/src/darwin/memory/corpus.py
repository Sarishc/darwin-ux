"""The Product Memory corpus: an explicit allowlist, loaded and normalised.

Only files named here are ever read. Not the repo, not globs: no .env, logs,
node_modules, git history, test fixtures, or anything a user typed. Adding a
source is a reviewed code change.
"""

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from darwin.signals import detectors

SourceKind = Literal["markdown", "ui_spec"]

REPO_ROOT = Path(__file__).resolve().parents[4]
MAX_SOURCE_BYTES = 512 * 1024


@dataclass(frozen=True)
class CorpusEntry:
    source_type: str  # repo_document | ui_spec
    source_key: str  # path relative to the repository root
    kind: SourceKind


DEFAULT_CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry("repo_document", "docs/PRODUCT.md", "markdown"),
    CorpusEntry("repo_document", "docs/ARCHITECTURE.md", "markdown"),
    CorpusEntry("repo_document", "docs/MUTATION_SAFETY.md", "markdown"),
    CorpusEntry("repo_document", "docs/DATA_PIPELINES.md", "markdown"),
    CorpusEntry("repo_document", "docs/EVALUATION_STRATEGY.md", "markdown"),
    CorpusEntry("repo_document", "docs/AWS_ARCHITECTURE.md", "markdown"),
    CorpusEntry("ui_spec", "frontend/src/ui-spec/generation-0.json", "ui_spec"),
)


@dataclass(frozen=True)
class SourceDocument:
    source_type: str
    source_key: str
    kind: SourceKind
    title: str
    content: str  # normalised
    metadata: dict[str, Any] = field(default_factory=dict)


class CorpusError(ValueError):
    pass


def normalize_text(raw: str) -> str:
    """Deterministic normalisation: NFC, \\n line endings, no trailing spaces, <= 1 blank line."""
    text = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip() + "\n"


def _markdown_title(content: str, fallback: str) -> str:
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()[:256]
    return fallback


def read_allowlisted(entry: CorpusEntry, root: Path) -> str:
    """Read one allowlisted file, refusing anything that resolves outside `root`."""
    root = root.resolve()
    path = (root / entry.source_key).resolve()
    if root not in path.parents:
        raise CorpusError(f"{entry.source_key}: outside the corpus root")
    if not path.is_file():
        raise CorpusError(f"{entry.source_key}: not a file")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise CorpusError(f"{entry.source_key}: larger than {MAX_SOURCE_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def load_entry(entry: CorpusEntry, root: Path = REPO_ROOT) -> SourceDocument:
    content = normalize_text(read_allowlisted(entry, root))
    if entry.kind == "markdown":
        title = _markdown_title(content, entry.source_key)
    else:
        title = f"UI Spec: {entry.source_key}"
    return SourceDocument(entry.source_type, entry.source_key, entry.kind, title, content)


def signal_definitions_document() -> SourceDocument:
    """A system-generated summary of the live detector rules, built from code constants."""
    rage_window = detectors.RAGE_CLICK_WINDOW.total_seconds()
    error_window = detectors.ERROR_BURST_WINDOW.total_seconds()
    content = normalize_text(
        f"""# Behaviour signal detector definitions

## rage_click (version {detectors.RAGE_CLICK_VERSION})

A rage click is detected when one anonymous session clicks the same component
at least {detectors.RAGE_CLICK_THRESHOLD} times within {rage_window:g} seconds. Counted
event types: {", ".join(sorted(detectors.RAGE_CLICK_EVENT_TYPES))}. The component comes
from payload.component and must be an identifier; clicks without one are skipped.

## error_burst (version {detectors.ERROR_BURST_VERSION})

An error burst is detected when one anonymous session produces at least
{detectors.ERROR_BURST_THRESHOLD} error events within {error_window:g} seconds. Counted
event types: {", ".join(sorted(detectors.ERROR_BURST_EVENT_TYPES))}.

## Shared rules

Detection is deterministic and ordered by occurred_at, not arrival order. Each
burst yields one signal whose evidence is the earliest qualifying run of events.
The signal_id is a UUID5 of the detector, its version, the session, the scope
and the evidence event ids, so replaying detection never duplicates signals.
"""
    )
    return SourceDocument(
        "system_generated",
        "signals/detector-definitions",
        "markdown",
        "Behaviour signal detector definitions",
        content,
    )


def load_corpus(
    entries: tuple[CorpusEntry, ...] = DEFAULT_CORPUS,
    root: Path = REPO_ROOT,
    include_generated: bool = True,
) -> list[SourceDocument]:
    documents = [load_entry(entry, root) for entry in entries]
    if include_generated:
        documents.append(signal_definitions_document())
    return documents
