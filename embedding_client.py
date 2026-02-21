"""Shared embedding client — local MLX embeddings for semantic search.

Uses bge-small-en-v1.5 via mlx-embedding-models (~130MB RAM).
Loads model lazily on first use. All agents can import this.

Usage:
    from shared.embedding_client import embed_text, semantic_search, cosine_similarity

    vec = embed_text("BTC drops on weekends")
    results = semantic_search("crypto weekend pattern", corpus_texts, top_k=5)
    sim = cosine_similarity(vec_a, vec_b)
"""
from __future__ import annotations

import json
import logging
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

_model = None


def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "llm_config.json"
    with open(cfg_path) as f:
        return json.load(f)


def get_model():
    """Lazy-load the embedding model (first call ~2s, then cached)."""
    global _model
    if _model is None:
        try:
            from mlx_embedding_models.embedding import EmbeddingModel
            cfg = _load_config()
            model_name = cfg.get("embedding", {}).get("model", "bge-small")
            _model = EmbeddingModel.from_registry(model_name)
            log.info("Loaded embedding model: %s", model_name)
        except ImportError:
            log.warning("mlx-embedding-models not installed — embeddings unavailable")
            raise
        except Exception as e:
            log.error("Failed to load embedding model: %s", e)
            raise
    return _model


def is_available() -> bool:
    """Check if embedding model can be loaded (without loading it)."""
    try:
        from mlx_embedding_models.embedding import EmbeddingModel  # noqa: F401
        return True
    except ImportError:
        return False


def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns 384-dim vector."""
    return get_model().encode([text])[0].tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts at once. More efficient than calling embed_text in a loop."""
    if not texts:
        return []
    return get_model().encode(texts).tolist()


def cosine_similarity(a, b) -> float:
    """Compute cosine similarity between two vectors."""
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm < 1e-9:
        return 0.0
    return float(dot / norm)


def semantic_search(query: str, corpus_texts: list[str], top_k: int = 10) -> list[tuple[int, float]]:
    """Search corpus by semantic similarity to query.

    Returns list of (index, similarity_score) sorted by relevance, descending.
    """
    if not corpus_texts:
        return []
    model = get_model()
    # Encode query + corpus together for efficiency
    all_texts = [query] + corpus_texts
    embeddings = model.encode(all_texts)
    query_emb = embeddings[0]
    corpus_embs = embeddings[1:]
    # Cosine similarities via normalized dot product
    query_norm = query_emb / (np.linalg.norm(query_emb) + 1e-9)
    corpus_norms = np.linalg.norm(corpus_embs, axis=1, keepdims=True) + 1e-9
    corpus_normed = corpus_embs / corpus_norms
    scores = corpus_normed @ query_norm
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [(int(i), float(scores[i])) for i in top_indices]
