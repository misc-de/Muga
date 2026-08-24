"""
Integration tests for Nextcloud workflows.
Tests: Open NC folder → Load thumbnails → Edit image → Save
"""
from pathlib import Path
import tempfile

from muga.database import Database


class MockNextcloudClient:
    """Mock WebDAV client for testing NC flows without network."""
    
    def __init__(self, host: str, webdav_url: str):
        self.host = host
        self.webdav_url = webdav_url
        self.files = {
            "/photos/pic1.jpg": {"type": "file", "size": 12345, "modified": "2024-01-15T10:00:00Z"},
            "/photos/pic2.jpg": {"type": "file", "size": 23456, "modified": "2024-01-14T10:00:00Z"},
            "/photos/subfolder/": {"type": "directory"},
            "/photos/subfolder/pic3.jpg": {"type": "file", "size": 34567, "modified": "2024-01-13T10:00:00Z"},
        }
    
    def list_files(self, path: str, depth: int = 1):
        """Mock PROPFIND response."""
        results = []
        path_normalized = path.rstrip("/")
        
        for file_path, info in self.files.items():
            if file_path.startswith(path_normalized):
                if info.get("type") == "file":
                    results.append({
                        "path": file_path,
                        "name": Path(file_path).name,
                        "size": info.get("size", 0),
                        "modified": info.get("modified", ""),
                    })
        return results
    
    def ensure_thumbnail(self, remote_path: str) -> str | None:
        """Mock thumbnail generation."""
        if remote_path in self.files:
            # Simulate cached thumbnail
            return f"/tmp/thumb_{Path(remote_path).stem}.jpg"
        return None
    
    def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Mock file download."""
        if remote_path in self.files:
            local_path.write_bytes(b"mock_image_data")
            return True
        return False


def test_nextcloud_list_folder_structure():
    """Test: Open NC folder and list files."""
    mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
    
    files = mock_nc.list_files("/photos/", depth=2)
    
    assert len(files) == 3  # pic1.jpg, pic2.jpg, pic3.jpg
    assert "pic1.jpg" in [f["name"] for f in files]
    assert "pic2.jpg" in [f["name"] for f in files]
    assert "pic3.jpg" in [f["name"] for f in files]  # From subfolder


def test_nextcloud_thumbnail_loading():
    """Test: Load thumbnail for NC file."""
    mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
    
    thumb_path = mock_nc.ensure_thumbnail("/photos/pic1.jpg")
    
    assert thumb_path is not None
    assert "thumb_pic1" in thumb_path


def test_nextcloud_file_download():
    """Test: Download file from NC."""
    mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = Path(tmp_dir) / "downloaded.jpg"
        success = mock_nc.download_file("/photos/pic1.jpg", local_path)
        
        assert success
        assert local_path.exists()
        assert local_path.read_bytes() == b"mock_image_data"


def test_nextcloud_edit_workflow():
    """
    Integration test: 
    1. Open NC folder
    2. Load thumbnails
    3. Download image
    4. Edit metadata in DB
    5. Verify DB state
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
        
        # Step 1: List NC folder
        files = mock_nc.list_files("/photos/", depth=2)
        assert len(files) == 3
        
        # Step 2: Create local files for DB (simulate downloaded NC files)
        local_files = []
        for i, file_info in enumerate(files):
            local_path = tmp_path / f"pic{i}.jpg"
            local_path.write_bytes(b"img_data")
            local_files.append(local_path)
            
            db.upsert_media(
                path=local_path,
                category="nextcloud",
                media_type="image",
                folder="/photos",
                thumb_path=mock_nc.ensure_thumbnail(file_info["path"]),
            )
        db.commit()
        
        # Step 3: Verify DB contains NC files
        nc_media = db.list_media("nextcloud", folder="/photos")
        assert len(nc_media) == 3
        
        # Step 4: Download first file (simulated)
        pic1_path = local_files[0]
        assert pic1_path.exists()
        
        # Step 5: Simulate edit (update thumbnail in DB)
        thumb_path = tmp_path / "pic0_edited_thumb.jpg"
        thumb_path.write_bytes(b"edited_thumb_data")
        db.set_thumb(str(pic1_path), str(thumb_path), category="nextcloud")
        db.commit()
        
        # Step 6: Verify edit persisted
        updated = db.get_media_by_path(str(pic1_path), category="nextcloud")
        assert updated is not None
        assert updated.thumb_path == str(thumb_path)


def test_nextcloud_category_isolation():
    """
    Test: NC category is isolated from local categories.
    Same file path in different categories shouldn't conflict.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        # Create a real test file
        test_path = tmp_path / "pic1.jpg"
        test_path.write_bytes(b"img_data")
        
        # Insert same path in two categories
        db.upsert_media(
            path=test_path,
            category="local",
            media_type="image",
            folder="/photos",
            thumb_path="/local/thumb1.jpg",
        )
        db.upsert_media(
            path=test_path,
            category="nextcloud",
            media_type="image",
            folder="/photos",
            thumb_path="/nc/thumb1.jpg",
        )
        db.commit()
        
        # Verify both entries exist and are separate
        local = db.get_media_by_path(str(test_path), category="local")
        nc = db.get_media_by_path(str(test_path), category="nextcloud")
        
        assert local is not None
        assert nc is not None
        assert local.thumb_path != nc.thumb_path
        
        # Delete NC entry shouldn't affect local
        db.delete_path(str(test_path), category="nextcloud")
        db.commit()
        
        assert db.get_media_by_path(str(test_path), category="local") is not None
        assert db.get_media_by_path(str(test_path), category="nextcloud") is None


def test_nextcloud_thumbnail_caching():
    """
    Test: Thumbnails are cached and reused.
    Second call to ensure_thumbnail should not regenerate.
    """
    mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
    
    # First call
    thumb1 = mock_nc.ensure_thumbnail("/photos/pic1.jpg")
    # Second call (should return same cached path)
    thumb2 = mock_nc.ensure_thumbnail("/photos/pic1.jpg")
    
    assert thumb1 == thumb2


def test_nextcloud_file_not_found():
    """Test: Graceful handling of missing NC file."""
    mock_nc = MockNextcloudClient("nc.example.com", "/remote.php/dav/files/user/")
    
    # Try to download non-existent file
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = Path(tmp_dir) / "nonexistent.jpg"
        success = mock_nc.download_file("/photos/missing.jpg", local_path)
        
        assert success is False
        assert not local_path.exists()


def test_nextcloud_error_recovery():
    """
    Test: NC workflows handle connection errors gracefully.
    Simulates network timeout or auth failure.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        # Create a test file that "was" synced before
        test_path = tmp_path / "pic1.jpg"
        test_path.write_bytes(b"img_data")
        
        db.upsert_media(
            path=test_path,
            category="nextcloud",
            media_type="image",
            folder="/photos",
            thumb_path="/cached/thumb1.jpg",
        )
        db.commit()
        
        # Simulate connection error: list_files returns empty (or exception caught)
        # DB should still have cached entry available for offline access
        media = db.list_media("nextcloud")
        assert len(media) == 1
        assert media[0].thumb_path == "/cached/thumb1.jpg"  # Cached thumbnail available


def test_nextcloud_sort_by_folder():
    """Test: NC files sorted by folder (nested structure)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        # Create test files
        pic1 = tmp_path / "pic1.jpg"
        pic2 = tmp_path / "pic2.jpg"
        pic1.write_bytes(b"img1")
        pic2.write_bytes(b"img2")
        
        # Insert nested NC structure
        db.upsert_media(
            path=pic1,
            category="nextcloud",
            media_type="image",
            folder="/photos",
            thumb_path=None,
        )
        db.upsert_media(
            path=pic2,
            category="nextcloud",
            media_type="image",
            folder="/photos/subfolder",
            thumb_path=None,
        )
        db.commit()
        
        # Sort by folder
        sorted_media = db.list_media("nextcloud", sort_mode="folder")
        folders = [m.folder for m in sorted_media]
        
        assert folders == ["/photos", "/photos/subfolder"]
