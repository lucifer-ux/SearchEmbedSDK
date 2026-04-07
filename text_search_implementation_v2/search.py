# text_search_implementation_v2/search.py
import base64
import json
import re
from typing import Any

from rapidfuzz import fuzz
from text_search_implementation_v2.db import (
    get_conn,
    get_file_metadata_by_ids,
    get_fts_content_by_ids,
    init_db,
    query_fts,
    query_fts_trigram,
    trigram_supported,
)

# retrieval tunables
FTS_CANDIDATE_LIMIT = 80
RRF_K = 60.0

# filename fuzzy tunables
FUZZY_FILENAME_THRESHOLD = 70
EXACT_FILENAME_BOOST = 5.0
FUZZY_FILENAME_BOOST = 2.5
FUZZY_FILENAME_BOOST_CAP = 1.25

# chunk selection tunables
SECOND_CHUNK_CONFIDENCE_RATIO = 0.82


def _normalize_query_for_fts(q: str) -> str:
    tokens = re.findall(r"\b\w+\b", q)
    if not tokens:
        return ""
    return " OR ".join(t + "*" for t in tokens)


def _normalize_query_for_trigram(q: str) -> str:
    # Trigram FTS lane is resilient on plain token OR queries.
    tokens = [t for t in re.findall(r"\b\w+\b", (q or "").lower()) if len(t) >= 3]
    if not tokens:
        return ""
    return " OR ".join(tokens)


class TextSearchEngineV2:
    def __init__(self):
        init_db()
        self._trigram_enabled = trigram_supported()

    def _estimate_tokens(self, text: str) -> int:
        words = re.findall(r"\S+", text or "")
        if not words:
            return 0
        return max(1, int(round(len(words) * 1.35)))

    def _trim_to_token_budget(self, text: str, token_budget: int) -> str:
        if token_budget <= 0:
            return ""
        if self._estimate_tokens(text) <= token_budget:
            return text

        words = re.findall(r"\S+", text or "")
        if not words:
            return ""

        keep_words = max(1, min(len(words), int(round(token_budget / 1.35))))
        trimmed = " ".join(words[:keep_words]).strip()
        return trimmed + " ..."

    def _adaptive_chunk_config(self, text_len: int, chunk_chars: int, chunk_overlap: int) -> tuple[int, int]:
        requested_chars = max(200, int(chunk_chars))
        requested_overlap = max(0, int(chunk_overlap))

        if text_len <= 1200:
            cap = 520
            overlap_cap = 60
        elif text_len <= 5000:
            cap = 780
            overlap_cap = 100
        else:
            cap = 950
            overlap_cap = 140

        cc = min(requested_chars, cap)
        ov = min(requested_overlap, max(0, min(overlap_cap, cc // 3)))
        return cc, ov

    def _split_chunks(self, text: str, chunk_chars: int, chunk_overlap: int) -> list[dict[str, Any]]:
        if not text:
            return []
        step = max(1, chunk_chars - chunk_overlap)
        chunks: list[dict[str, Any]] = []
        i = 0
        idx = 0
        while i < len(text):
            end = min(len(text), i + chunk_chars)
            chunk = text[i:end].strip()
            if chunk:
                chunks.append({"index": idx, "start": i, "end": end, "text": chunk})
                idx += 1
            if end >= len(text):
                break
            i += step
        return chunks

    def _chunk_score(self, chunk_text: str, tokens: list[str], query_text: str) -> float:
        text = (chunk_text or "").lower()
        if not text:
            return 0.0

        if not tokens:
            return 1.0

        words = re.findall(r"\b\w+\b", text)
        word_count = max(1, len(words))

        counts = {t: text.count(t) for t in tokens}
        hit_total = float(sum(counts.values()))
        unique_hits = sum(1 for _, c in counts.items() if c > 0)
        density = hit_total / float(word_count)

        positions: list[int] = []
        for t in tokens:
            m = re.search(rf"\b{re.escape(t)}\b", text)
            if m:
                positions.append(m.start())
        proximity = 0.0
        if len(positions) >= 2:
            span = max(positions) - min(positions)
            proximity = 1.0 / float(1 + span)

        phrase_bonus = 0.0
        q = (query_text or "").strip().lower()
        if q and len(q) >= 4 and q in text:
            phrase_bonus = 1.0

        return (
            hit_total * 1.0
            + float(unique_hits) * 0.35
            + density * 10.0
            + proximity * 250.0
            + phrase_bonus * 2.5
        )

    def _select_chunks(
        self,
        file_id: int,
        content: str,
        tokens: list[str],
        query_text: str,
        chunk_chars: int,
        chunk_overlap: int,
        max_chunks_per_doc: int,
        max_context_tokens_per_result: int | None,
    ) -> dict[str, Any] | None:
        if not content:
            return None

        adaptive_chars, adaptive_overlap = self._adaptive_chunk_config(
            len(content), chunk_chars=chunk_chars, chunk_overlap=chunk_overlap
        )
        chunks = self._split_chunks(content, adaptive_chars, adaptive_overlap)
        if not chunks:
            return None

        scored: list[tuple[float, dict[str, Any]]] = []
        for c in chunks:
            score = self._chunk_score(str(c["text"]), tokens=tokens, query_text=query_text)
            scored.append((float(score), c))
        scored.sort(key=lambda x: (x[0], -int(x[1]["index"])), reverse=True)

        selected: list[dict[str, Any]] = []
        first_score = float(scored[0][0])
        max_chunks = max(1, int(max_chunks_per_doc))

        for idx, (score, c) in enumerate(scored):
            if idx == 0:
                selected.append(c)
                continue
            if len(selected) >= max_chunks:
                break

            if first_score > 0 and score >= first_score * SECOND_CHUNK_CONFIDENCE_RATIO:
                selected.append(c)
            else:
                break

        budget = None
        if max_context_tokens_per_result is not None:
            budget = max(1, int(max_context_tokens_per_result))

        rendered_chunks: list[str] = []
        rendered_meta: list[dict[str, Any]] = []
        used_tokens = 0

        for c in selected:
            text = str(c["text"])
            if budget is None:
                rendered_chunks.append(text)
                rendered_meta.append(c)
                continue

            remain = budget - used_tokens
            if remain <= 0:
                break

            estimate = self._estimate_tokens(text)
            if estimate <= remain:
                rendered_chunks.append(text)
                rendered_meta.append(c)
                used_tokens += estimate
            elif not rendered_chunks:
                trimmed = self._trim_to_token_budget(text, remain)
                if trimmed:
                    rendered_chunks.append(trimmed)
                    rendered_meta.append(c)
                    used_tokens += self._estimate_tokens(trimmed)
                break
            else:
                break

        if not rendered_chunks:
            # preserve behavior and always return one chunk
            c = selected[0]
            text = str(c["text"])
            if budget is not None:
                text = self._trim_to_token_budget(text, budget)
            rendered_chunks = [text]
            rendered_meta = [c]

        first = rendered_meta[0]
        chunk_ids = [
            self._encode_chunk_id(file_id, int(c["index"]), adaptive_chars, adaptive_overlap)
            for c in rendered_meta
        ]
        return {
            "chunk": "\n\n---\n\n".join(rendered_chunks).strip(),
            "chunk_index": int(first["index"]),
            "chunk_id": chunk_ids[0],
            "chunk_total": len(chunks),
            "matched_chunks": len(rendered_chunks),
            "chunk_indices": [int(c["index"]) for c in rendered_meta],
            "chunk_ids": chunk_ids,
            "chunk_chars_used": adaptive_chars,
            "chunk_overlap_used": adaptive_overlap,
        }

    def _encode_chunk_id(self, file_id: int, chunk_index: int, chunk_chars: int, chunk_overlap: int) -> str:
        payload = {
            "fid": int(file_id),
            "idx": int(chunk_index),
            "cc": int(chunk_chars),
            "ov": int(chunk_overlap),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_chunk_id(self, chunk_id: str) -> dict[str, int]:
        pad = "=" * ((4 - len(chunk_id) % 4) % 4)
        data = base64.urlsafe_b64decode((chunk_id + pad).encode("ascii"))
        obj = json.loads(data.decode("utf-8"))
        return {
            "fid": int(obj["fid"]),
            "idx": int(obj["idx"]),
            "cc": int(obj["cc"]),
            "ov": int(obj["ov"]),
        }

    def _is_noisy_query(self, query: str, tokens: list[str]) -> bool:
        if not tokens:
            return False
        if len(tokens) <= 2:
            return True
        if any(len(t) <= 3 for t in tokens):
            return True
        if re.search(r"[^\w\s]", query):
            return True
        return False

    def _lane_weights(self, retrieval_mode: str, query: str, tokens: list[str]) -> tuple[float, float]:
        mode = (retrieval_mode or "contextcore_hybrid").strip().lower()
        if mode == "bm25_only":
            return 1.0, 0.0
        if mode == "trigram_only":
            return 0.0, 1.0 if self._trigram_enabled else 0.0

        porter = 1.0
        trigram = 0.20 if self._trigram_enabled else 0.0
        if self._is_noisy_query(query, tokens) and self._trigram_enabled:
            porter = 1.0
            trigram = 0.55
        return porter, trigram

    def _merge_candidates_rrf(
        self,
        porter_rows: list[Any],
        trigram_rows: list[Any],
        porter_weight: float,
        trigram_weight: float,
    ) -> dict[int, float]:
        scores: dict[int, float] = {}

        if porter_weight > 0:
            for rank, row in enumerate(porter_rows, start=1):
                fid = int(row["id"])
                scores[fid] = scores.get(fid, 0.0) + (porter_weight / (RRF_K + float(rank)))

        if trigram_weight > 0:
            for rank, row in enumerate(trigram_rows, start=1):
                fid = int(row["id"])
                scores[fid] = scores.get(fid, 0.0) + (trigram_weight / (RRF_K + float(rank)))

        return scores

    def search(
        self,
        query: str,
        categories=None,
        top_k: int = 20,
        include_metadata: bool = False,
        chunk_chars: int = 900,
        chunk_overlap: int = 120,
        exclude_sources: set[str] | None = None,
        retrieval_mode: str = "contextcore_hybrid",
        max_context_tokens_per_result: int | None = None,
        max_chunks_per_doc: int = 1,
    ):
        if not query or not query.strip():
            return []

        q_norm = query.strip().lower()
        tokens = re.findall(r"\b\w+\b", q_norm)
        excluded = {str(p) for p in (exclude_sources or set())}
        category_filter = {str(c).lower() for c in categories} if categories else None

        # 1) Exact filename path (chunk-consistent output)
        conn = get_conn()
        row = conn.execute(
            "SELECT id, path, filename, category, content FROM files WHERE LOWER(filename) = ? LIMIT 1",
            (q_norm,),
        ).fetchone()
        conn.close()

        if row:
            if row["path"] not in excluded:
                if category_filter is None or str(row["category"]).lower() in category_filter:
                    chunk_payload = self._select_chunks(
                        file_id=int(row["id"]),
                        content=str(row["content"] or ""),
                        tokens=tokens,
                        query_text=q_norm,
                        chunk_chars=chunk_chars,
                        chunk_overlap=chunk_overlap,
                        max_chunks_per_doc=max_chunks_per_doc,
                        max_context_tokens_per_result=max_context_tokens_per_result,
                    )
                    if chunk_payload:
                        item = {
                            "path": row["path"],
                            "category": row["category"],
                            "score": EXACT_FILENAME_BOOST,
                            "chunk": chunk_payload["chunk"],
                            "chunk_id": chunk_payload["chunk_id"],
                            "chunk_index": chunk_payload["chunk_index"],
                            "chunk_total": chunk_payload["chunk_total"],
                            "matched_chunks": chunk_payload["matched_chunks"],
                        }
                        if include_metadata:
                            item["filename"] = row["filename"]
                            item["file_id"] = int(row["id"])
                            item["chunk_indices"] = chunk_payload["chunk_indices"]
                            item["chunk_ids"] = chunk_payload["chunk_ids"]
                            item["chunk_chars_used"] = chunk_payload["chunk_chars_used"]
                            item["chunk_overlap_used"] = chunk_payload["chunk_overlap_used"]
                        return [item]

        # 2) Lane retrieval + hybrid RRF merge
        porter_q = _normalize_query_for_fts(q_norm)
        trigram_q = _normalize_query_for_trigram(q_norm)
        porter_weight, trigram_weight = self._lane_weights(retrieval_mode=retrieval_mode, query=q_norm, tokens=tokens)

        porter_rows = query_fts(porter_q, limit=FTS_CANDIDATE_LIMIT) if porter_q and porter_weight > 0 else []
        trigram_rows = (
            query_fts_trigram(trigram_q, limit=FTS_CANDIDATE_LIMIT)
            if trigram_q and trigram_weight > 0 and self._trigram_enabled
            else []
        )

        merged_scores = self._merge_candidates_rrf(
            porter_rows=porter_rows,
            trigram_rows=trigram_rows,
            porter_weight=porter_weight,
            trigram_weight=trigram_weight,
        )

        # Fallback: keep previous fuzzy filename behavior if no lexical candidates.
        if not merged_scores and tokens:
            conn = get_conn()
            all_files = conn.execute("SELECT id, path, filename, category FROM files").fetchall()
            conn.close()
            results = []
            for row in all_files:
                if row["path"] in excluded:
                    continue
                if category_filter is not None and str(row["category"]).lower() not in category_filter:
                    continue

                fname = str(row["filename"] or "").lower()
                best_token_score = max((fuzz.partial_ratio(t, fname) for t in tokens), default=0)
                if best_token_score < FUZZY_FILENAME_THRESHOLD:
                    continue

                content = get_fts_content_by_ids([row["id"]]).get(int(row["id"]), "")
                chunk_payload = self._select_chunks(
                    file_id=int(row["id"]),
                    content=content,
                    tokens=tokens,
                    query_text=q_norm,
                    chunk_chars=chunk_chars,
                    chunk_overlap=chunk_overlap,
                    max_chunks_per_doc=max_chunks_per_doc,
                    max_context_tokens_per_result=max_context_tokens_per_result,
                )
                if not chunk_payload:
                    continue

                item = {
                    "path": row["path"],
                    "category": row["category"],
                    "score": min(1.0, float(best_token_score) / 100.0),
                    "chunk": chunk_payload["chunk"],
                    "chunk_id": chunk_payload["chunk_id"],
                    "chunk_index": chunk_payload["chunk_index"],
                    "chunk_total": chunk_payload["chunk_total"],
                    "matched_chunks": chunk_payload["matched_chunks"],
                }
                if include_metadata:
                    item["filename"] = row["filename"]
                    item["file_id"] = int(row["id"])
                    item["chunk_indices"] = chunk_payload["chunk_indices"]
                    item["chunk_ids"] = chunk_payload["chunk_ids"]
                    item["chunk_chars_used"] = chunk_payload["chunk_chars_used"]
                    item["chunk_overlap_used"] = chunk_payload["chunk_overlap_used"]
                results.append(item)

            results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
            return results[: max(1, int(top_k))]

        if not merged_scores:
            return []

        ranked_ids = [
            fid for fid, _ in sorted(merged_scores.items(), key=lambda x: (x[1], -x[0]), reverse=True)
        ]

        id_to_meta = get_file_metadata_by_ids(ranked_ids)
        id_to_content = get_fts_content_by_ids(ranked_ids)

        results = []
        for fid in ranked_ids:
            meta = id_to_meta.get(fid)
            if not meta:
                continue
            if meta["path"] in excluded:
                continue
            if category_filter is not None and str(meta["category"]).lower() not in category_filter:
                continue

            base = float(merged_scores.get(fid, 0.0))
            fname = str(meta.get("filename") or "").lower()
            best_token_score = max((fuzz.partial_ratio(t, fname) for t in tokens), default=0)
            fuzzy_boost = 0.0
            if best_token_score >= FUZZY_FILENAME_THRESHOLD:
                fuzzy_boost = min(
                    FUZZY_FILENAME_BOOST_CAP,
                    FUZZY_FILENAME_BOOST * (float(best_token_score) / 100.0),
                )

            content = id_to_content.get(int(fid), "")
            chunk_payload = self._select_chunks(
                file_id=int(fid),
                content=content,
                tokens=tokens,
                query_text=q_norm,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
                max_chunks_per_doc=max_chunks_per_doc,
                max_context_tokens_per_result=max_context_tokens_per_result,
            )
            if not chunk_payload:
                continue

            item = {
                "path": meta["path"],
                "category": meta["category"],
                "score": float(base + fuzzy_boost),
                "chunk": chunk_payload["chunk"],
                "chunk_id": chunk_payload["chunk_id"],
                "chunk_index": chunk_payload["chunk_index"],
                "chunk_total": chunk_payload["chunk_total"],
                "matched_chunks": chunk_payload["matched_chunks"],
            }
            if include_metadata:
                item["filename"] = meta["filename"]
                item["file_id"] = int(fid)
                item["chunk_indices"] = chunk_payload["chunk_indices"]
                item["chunk_ids"] = chunk_payload["chunk_ids"]
                item["chunk_chars_used"] = chunk_payload["chunk_chars_used"]
                item["chunk_overlap_used"] = chunk_payload["chunk_overlap_used"]
            results.append(item)

        results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return results[: max(1, int(top_k))]

    def get_neighbors(
        self,
        chunk_id: str,
        direction: str = "next",
        count: int = 1,
    ) -> dict[str, Any]:
        data = self._decode_chunk_id(chunk_id)
        fid = data["fid"]
        idx = data["idx"]
        chunk_chars = data["cc"]
        chunk_overlap = data["ov"]

        meta = get_file_metadata_by_ids([fid]).get(fid)
        if not meta:
            return {"ok": False, "error": "file_not_found"}

        content = get_fts_content_by_ids([fid]).get(fid, "")
        chunks = self._split_chunks(content, chunk_chars, chunk_overlap)
        if not chunks:
            return {"ok": False, "error": "no_chunks"}

        direction = direction.lower().strip()
        if direction not in {"next", "prev"}:
            return {"ok": False, "error": "invalid_direction"}

        out = []
        step = 1 if direction == "next" else -1
        cur = idx
        for _ in range(max(1, int(count))):
            cur += step
            if cur < 0 or cur >= len(chunks):
                break
            c = chunks[cur]
            out.append(
                {
                    "path": meta["path"],
                    "category": meta["category"],
                    "chunk": c["text"],
                    "chunk_id": self._encode_chunk_id(fid, c["index"], chunk_chars, chunk_overlap),
                    "chunk_index": c["index"],
                    "chunk_total": len(chunks),
                }
            )

        return {"ok": True, "results": out, "source": meta["path"], "direction": direction}
