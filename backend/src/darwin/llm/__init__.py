"""DarwinUX's language-model boundary (Step 9).

`port` holds the DarwinUX-owned request/result shapes and the `LLMProvider`
protocol; `fake` holds the deterministic provider used by tests, evaluation
and offline development. No provider SDK is imported anywhere in DarwinUX:
a real adapter, when one is chosen (OPEN_QUESTIONS.md N1), lives beside
`fake` and translates these shapes to that provider's API.
"""
