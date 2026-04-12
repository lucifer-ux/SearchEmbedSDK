#!/usr/bin/env python3
"""
Test deletion tracking functionality for all modalities.
This verifies that when files are deleted, their entries are removed from databases.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))


def test_text_deletion():
    """Test deletion of text file entries."""
    print("\n📝 Testing text file deletion...")
    from text_search_implementation_v2.db import get_conn, init_db, upsert_file
    from unimain import _delete_text_file
    
    init_db()
    
    # Create a unique test path
    import time
    test_path = f"/tmp/test_delete_text_{int(time.time())}.txt"
    upsert_file(test_path, "Test file content for deletion", "utf-8", "text", None)
    
    # Verify it exists
    conn = get_conn()
    row = conn.execute("SELECT id FROM files WHERE path = ?", (test_path,)).fetchone()
    assert row is not None, "Text file should exist after upsert"
    file_id = row["id"]
    conn.close()
    
    # Delete it
    result = _delete_text_file(test_path)
    assert result, "Deletion should succeed"
    
    # Verify it's gone
    conn = get_conn()
    row = conn.execute("SELECT id FROM files WHERE path = ?", (test_path,)).fetchone()
    assert row is None, "Text file should be deleted"
    conn.close()
    
    print("✅ Text file deletion works")


def test_image_deletion():
    """Test deletion of image file entries."""
    print("\n🖼️  Testing image file deletion...")
    try:
        from image_search_implementation_v2.db import get_conn as img_get_conn, init_db as img_init_db, upsert_image
        from unimain import _delete_image_file
        
        img_init_db()
        
        # Create a unique test image entry
        import time
        test_path = f"/tmp/test_delete_{int(time.time())}.jpg"
        success, image_id = upsert_image(test_path, f"test_delete_{int(time.time())}.jpg", time.time(), "OCR test content")
        assert success, "Image upsert should succeed"
        
        # Verify it exists
        conn = img_get_conn()
        row = conn.execute("SELECT id FROM images WHERE path = ?", (test_path,)).fetchone()
        assert row is not None, "Image should exist after upsert"
        conn.close()
        
        # Delete it
        result = _delete_image_file(test_path)
        assert result, "Deletion should succeed"
        
        # Verify it's gone
        conn = img_get_conn()
        row = conn.execute("SELECT id FROM images WHERE path = ?", (test_path,)).fetchone()
        assert row is None, "Image should be deleted"
        conn.close()
        
        print("✅ Image file deletion works")
    except ImportError as e:
        print(f"⏭️  Image test skipped: {e}")


def test_code_deletion():
    """Test deletion of code file entries and chunks."""
    print("\n💻 Testing code file deletion...")
    from unimain import _code_db_conn, _delete_code_file
    
    # Create a unique test path to avoid conflicts
    import time
    test_path = f"/tmp/test_delete_{int(time.time())}.py"
    conn = _code_db_conn()
    
    # Clean up any existing entries first
    conn.execute("DELETE FROM project_files WHERE file_path = ?", (test_path,))
    conn.execute("DELETE FROM code_chunks WHERE file_path = ?", (test_path,))
    conn.commit()
    
    conn.execute("""
        INSERT INTO project_files (file_path, repo_path, relative_path, extension, line_count, size_bytes, last_modified)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (test_path, "/tmp", f"test_delete_{int(time.time())}.py", ".py", 10, 100, time.time()))
    
    conn.execute("""
        INSERT INTO code_chunks (repo_path, file_path, relative_path, chunk_index, start_line, end_line, chunk_text, chunk_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, ("/tmp", test_path, f"test_delete_{int(time.time())}.py", 0, 1, 10, "def test(): pass", f"hash{int(time.time())}"))
    
    conn.commit()
    conn.close()
    
    # Verify entries exist
    conn = _code_db_conn()
    row = conn.execute("SELECT file_path FROM project_files WHERE file_path = ?", (test_path,)).fetchone()
    assert row is not None, "Code file should exist"
    chunk_row = conn.execute("SELECT id FROM code_chunks WHERE file_path = ?", (test_path,)).fetchone()
    assert chunk_row is not None, "Code chunk should exist"
    conn.close()
    
    # Delete it
    result = _delete_code_file(test_path)
    assert result, "Deletion should succeed"
    
    # Verify entries are gone
    conn = _code_db_conn()
    row = conn.execute("SELECT file_path FROM project_files WHERE file_path = ?", (test_path,)).fetchone()
    assert row is None, "Code file should be deleted"
    chunk_row = conn.execute("SELECT id FROM code_chunks WHERE file_path = ?", (test_path,)).fetchone()
    assert chunk_row is None, "Code chunk should be deleted"
    conn.close()
    
    print("✅ Code file deletion works")


def main():
    """Run all deletion tests."""
    print("🧪 Testing deletion tracking functionality...")
    
    try:
        test_text_deletion()
        test_image_deletion()
        test_code_deletion()
        
        print("\n✅ All deletion tracking tests passed!")
        return 0
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
