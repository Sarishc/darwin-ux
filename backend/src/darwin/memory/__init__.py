"""Product Memory: allowlisted documents -> chunks -> embeddings -> pgvector retrieval.

Retrieval only: nothing here calls a chat/completion model or builds prompts.
Retrieved text is untrusted data, never instructions.
"""
