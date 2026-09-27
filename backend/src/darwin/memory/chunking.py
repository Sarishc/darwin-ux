"""Deterministic chunking: same document + same config -> identical chunks.

Markdown: split on headings (outside code fences) into sections; each chunk
is prefixed with its heading path ("Validation Pipeline > The Sandbox") so it
carries its own context. A section larger than `max_chars` is split on
paragraph boundaries (and, for a single huge paragraph, on whitespace).

No character overlap between chunks: the heading prefix already gives each
chunk its context, and overlap would make the chunking comparison harder to
read. Sizes are in characters (~4 characters per English token): no model
tokenizer is chosen yet, so exact token counts would be false precision.

UI Spec (JSON): one chunk describing the page, then one per spec section,
listing its components as readable lines — never arbitrary character cuts.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from darwin.memory.corpus import SourceDocument

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class ChunkerConfig:
    label: str
    max_chars: int

    def __post_init__(self) -> None:
        if self.max_chars < 200:
            raise ValueError("max_chars must be at least 200")

    def name(self, kind: str) -> str:
        return f"{kind}-sections:v1:{self.max_chars}"


# ~250, ~500 and ~750 tokens. STANDARD is the documented default (the target
# range in RAG_ARCHITECTURE.md); SMALL and LARGE exist to be measured against it.
SMALL = ChunkerConfig("small", 1000)
STANDARD = ChunkerConfig("standard", 2000)
LARGE = ChunkerConfig("large", 3000)
CONFIGS = {c.label: c for c in (SMALL, STANDARD, LARGE)}


@dataclass(frozen=True)
class Chunk:
    section: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _split_long(body: str, limit: int) -> list[str]:
    """Pack paragraphs up to `limit`; split a single oversized paragraph on whitespace."""
    pieces: list[str] = []
    current = ""
    for paragraph in [p.strip() for p in body.split("\n\n") if p.strip()]:
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = paragraph
    if current:
        pieces.append(current)
    return pieces


def markdown_sections(content: str) -> list[tuple[str, str]]:
    """(heading path, body) pairs in document order. Headings inside code fences are text."""
    sections: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            # The document title (level 1) is recorded as metadata, not repeated per chunk.
            heading = " > ".join(title for level, title in path if level > 1) or "(introduction)"
            sections.append((heading[:256], text))
        body.clear()

    for line in content.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            path = [(lvl, title) for lvl, title in path if lvl < level]
            path.append((level, match.group(2).strip()))
        else:
            body.append(line)
    flush()
    return sections


def chunk_markdown(document: SourceDocument, config: ChunkerConfig) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, body in markdown_sections(document.content):
        prefix = f"{heading}\n\n" if heading != "(introduction)" else ""
        for piece in _split_long(body, max(config.max_chars - len(prefix), 100)):
            chunks.append(Chunk(heading, prefix + piece, {"section": heading}))
    return chunks


def _describe_component(component: dict[str, Any]) -> list[str]:
    kind = str(component.get("type", "?"))
    cid = str(component.get("id", "?"))
    if kind == "plan_grid":
        lines = [f"- plan_grid {cid} with {len(component.get('plans', []))} plans:"]
        for plan in component.get("plans", []):
            cta = plan.get("cta", {})
            features = "; ".join(plan.get("features", []))
            lines.append(
                f"  - plan_card {plan.get('id')}: {plan.get('name')!r}, {plan.get('price_label')}, "
                f"highlighted={plan.get('highlighted')}, features: {features}. "
                f"CTA button {cta.get('id')}: {cta.get('label')!r}, variant={cta.get('variant')}, "
                f"feedback={cta.get('feedback')}, action={cta.get('action')}"
            )
        return lines
    if kind == "signup_form":
        fields = ", ".join(
            f"{f.get('name')} ({f.get('label')!r})" for f in component.get("fields", [])
        )
        return [
            f"- signup_form {cid}: {component.get('title')!r}; fields: {fields}; "
            f"validation={component.get('validation')}, "
            f"error_display={component.get('error_display')}; "
            f"summary error: {component.get('summary_error_text')!r}; "
            f"submit: {component.get('submit_label')!r}"
        ]
    if kind == "button":
        return [
            f"- button {cid}: {component.get('label')!r}, variant={component.get('variant')}, "
            f"feedback={component.get('feedback')}, action={component.get('action')}"
        ]
    text = component.get("text", "")
    extra = f" level={component['level']}" if "level" in component else ""
    return [f"- {kind} {cid}{extra}: {text!r}"]


def chunk_ui_spec(document: SourceDocument, config: ChunkerConfig) -> list[Chunk]:
    """UI Specs are data; they are described as text for retrieval, never executed."""
    spec = json.loads(document.content)
    page = spec.get("page", {})
    generation = spec.get("generation")
    base_meta = {"generation": generation, "page": page.get("id")}
    header = (
        f"UI Spec version {spec.get('version')}, generation {generation}: "
        f"page {page.get('id')} ({page.get('title')!r})"
    )
    sections = page.get("sections", [])
    chunks = [
        Chunk(
            "(page)",
            f"{header}. Sections, in order: "
            + ", ".join(f"{s.get('id')} (visibility: {s.get('visibility')})" for s in sections)
            + ".",
            base_meta,
        )
    ]
    for section in sections:
        heading = f"section {section.get('id')}"
        intro = (
            f"{header}, section {section.get('id')} "
            f"(spacing {section.get('spacing')}, visibility {section.get('visibility')}):"
        )
        lines = [line for c in section.get("components", []) for line in _describe_component(c)]
        # Pack component lines up to the limit; a section never splits mid-line.
        current = intro
        for line in lines:
            if len(current) + 1 + len(line) > config.max_chars and current != intro:
                chunks.append(Chunk(heading, current, {**base_meta, "section": section.get("id")}))
                current = intro
            current = f"{current}\n{line}"
        chunks.append(Chunk(heading, current, {**base_meta, "section": section.get("id")}))
    return chunks


def chunk_document(document: SourceDocument, config: ChunkerConfig) -> list[Chunk]:
    if document.kind == "ui_spec":
        return chunk_ui_spec(document, config)
    return chunk_markdown(document, config)
