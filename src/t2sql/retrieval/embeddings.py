"""Table-level embeddings for schema retrieval, cached to disk.

Each table gets a single embedding built from its description, grain, and
column descriptions (from semantic/entities.yaml). Retrieval then compares a
question's embedding against these table vectors by cosine similarity.
"""

from __future__ import annotations

import hashlib
import pickle
from functools import lru_cache
from pathlib import Path

import numpy as np

from t2sql.semantic.models import Entity, SemanticLayer

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
EMBEDDINGS_CACHE_PATH = DATA_DIR / "embeddings.pkl"
MODEL_NAME = "BAAI/bge-small-en-v1.5"

# bge's retrieval models are trained asymmetrically: only the query side gets
# an instruction prefix, not the passages being searched over.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _related_metric_terms(layer: SemanticLayer) -> dict[str, set[str]]:
    """Table -> synonyms of every metric that requires joining that table.

    A table only used for its raw columns (e.g. "products") won't otherwise
    surface for revenue-flavored questions like "who spent the most" -- but
    it's exactly the kind of query the semantic layer's metric synonyms are
    meant to catch, so fold them into the tables that compute those metrics.
    """
    terms: dict[str, set[str]] = {name: set() for name in layer.entities}
    for metric in layer.metrics.values():
        for table in metric.requires_join:
            if table in terms:
                terms[table].update(metric.synonyms)
    return terms


def _table_document(name: str, entity: Entity, related_terms: set[str] = frozenset()) -> str:
    lines = [f"Table: {name}", f"Description: {entity.description.strip()}", f"Grain: {entity.grain}"]
    for column, description in entity.columns.items():
        lines.append(f"Column {column}: {description.strip()}")
    if related_terms:
        lines.append("Related terms: " + ", ".join(sorted(related_terms)))
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _load_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME)


def embed_query(question: str) -> np.ndarray:
    model = _load_model()
    vector = model.encode([QUERY_PREFIX + question], normalize_embeddings=True)[0]
    return np.asarray(vector, dtype=np.float32)


def get_table_embeddings(
    layer: SemanticLayer,
    cache_path: Path = EMBEDDINGS_CACHE_PATH,
    force_refresh: bool = False,
) -> tuple[list[str], np.ndarray]:
    """Return (table_names, vectors) with vectors[i] the embedding for table_names[i].

    Cached to disk keyed on a hash of the source documents plus the model name,
    so edits to entities.yaml/metrics.yaml or a model change transparently
    invalidate the cache.
    """
    table_names = sorted(layer.entities)
    related_terms = _related_metric_terms(layer)
    documents = [
        _table_document(name, layer.entities[name], related_terms[name]) for name in table_names
    ]
    content_hash = hashlib.sha256("\n---\n".join(documents).encode()).hexdigest()

    if not force_refresh and cache_path.exists():
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("content_hash") == content_hash and cached.get("model") == MODEL_NAME:
            return cached["tables"], cached["vectors"]

    model = _load_model()
    vectors = np.asarray(model.encode(documents, normalize_embeddings=True), dtype=np.float32)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(
            {
                "model": MODEL_NAME,
                "content_hash": content_hash,
                "tables": table_names,
                "vectors": vectors,
            },
            f,
        )
    return table_names, vectors
