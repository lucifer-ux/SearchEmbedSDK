from fastapi import FastAPI, Query, HTTPException
from pathlib import Path
from text_search_implementation_v2.search import TextSearchEngineV2
from text_search_implementation_v2.index_controller import full_scan, index_single_file
from text_search_implementation_v2.config import BASE_DIR
from text_search_implementation_v2.db import init_db

app = FastAPI(title="Text Search V2")

search_engine = None


@app.on_event("startup")
def startup():
    global search_engine
    init_db()
    search_engine = TextSearchEngineV2()
    print("🚀 Text Search V2 ready")


@app.get("/health")
def health():
    return {"status": "ok", "service": "text-search-v2"}


@app.get("/search")
def search(
    query: str = Query(..., min_length=1),
    top_k: int = Query(20, ge=1, le=100),
    retrieval_mode: str = Query("contextcore_hybrid"),
    max_context_tokens_per_result: int | None = Query(None, ge=1, le=4000),
    max_chunks_per_doc: int = Query(1, ge=1, le=4),
):
    if not search_engine:
        raise HTTPException(status_code=503, detail="Search engine not ready")

    results = search_engine.search(
        query=query,
        top_k=top_k,
        retrieval_mode=retrieval_mode,
        max_context_tokens_per_result=max_context_tokens_per_result,
        max_chunks_per_doc=max_chunks_per_doc,
    )

    return {
        "query": query,
        "count": len(results),
        "results": results
    }


@app.post("/index/scan")
def trigger_full_scan():
    full_scan()
    return {"status": "scan_started"}


@app.post("/index/file")
def trigger_single_file(path: str):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")

    index_single_file(p)
    return {"status": "index_started", "file": path}
