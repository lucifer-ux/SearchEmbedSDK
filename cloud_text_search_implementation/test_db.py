import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_text_search_implementation.db import (
    STATE_FETCHED,
    claim_next_file,
    get_conn,
    init_db,
    insert_buffer,
    insert_manifest,
    manifest_counts,
    read_buffered_content,
    update_manifest_state,
    upsert_document,
    upsert_document_chunks,
)
from cloud_text_search_implementation.embeddings import embed_text
from cloud_text_search_implementation.search import search_cloud_text

init_db()
conn = get_conn()
with conn:
    conn.execute("DELETE FROM cloud_chunk_vectors")
    conn.execute("DELETE FROM cloud_chunks_fts")
    conn.execute("DELETE FROM cloud_chunks")
    conn.execute("DELETE FROM cloud_documents_fts")
    conn.execute("DELETE FROM cloud_documents")
    conn.execute("DELETE FROM buffer")
    conn.execute("DELETE FROM cloud_manifest")

file_info = {
    "Path": "notes/text.txt",
    "Size": 1234,
    "ModTime": "2024-01-01T12:00:00",
}
remote = "contextcore_drive"

with conn:
    insert_manifest(conn, file_info, remote)

row = claim_next_file(conn, remote=remote)
print("Claimed:", row)

if row:
    with conn:
        insert_buffer(conn, row, "hello ", chunk_index=0)
        insert_buffer(conn, row, "world", chunk_index=1)
        text = read_buffered_content(conn, row)
        doc_id = upsert_document(conn, row, text)
        upsert_document_chunks(conn, row, doc_id, text, embed_fn=embed_text)
        ok = update_manifest_state(conn, row, STATE_FETCHED)
    print("Update success:", ok)

print("Counts:", manifest_counts(conn, remote))
docs = conn.execute("SELECT path, remote, filename FROM cloud_documents").fetchall()
print("Documents:", [dict(d) for d in docs])
conn.close()

print("Search:", search_cloud_text("hello", top_k=5))
