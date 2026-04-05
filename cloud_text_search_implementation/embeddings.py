import hashlib
import math
import re

VECTOR_DIM = 256
_TOKEN_RE = re.compile(r"\b\w+\b")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def embed_text(text: str, dim: int = VECTOR_DIM) -> list[float]:
    tokens = _tokenize(text)
    vec = [0.0] * dim
    if not tokens:
        vec[0] = 1.0
        return vec

    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=16).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) else -1.0
        weight = 1.0 + (int.from_bytes(digest[5:7], "little") % 100) / 400.0
        vec[idx] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]
