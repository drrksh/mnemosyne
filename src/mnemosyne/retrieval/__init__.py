"""
Retrieval layer: turning "what is the developer doing right now?" into
a ranked list of the memories that matter.

Hybrid by design — lexical (BM25F) and semantic (embeddings) relevance
fused with importance, confidence, recency, scope affinity and
reinforcement — and explainable: every score carries its breakdown.
"""
