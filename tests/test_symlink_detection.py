"""Test symlink detection in scanner."""
import tempfile
from pathlib import Path

from muga.database import Database
from muga.scanner import MediaScanner
from muga.thumbnails import Thumbnailer


def test_scanner_skips_symlinks():
    """Test: Scanner ignores symlinks to prevent loops."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create real files
        photos_dir = tmp_path / "photos"
        photos_dir.mkdir()
        
        real_file = photos_dir / "photo.jpg"
        real_file.write_bytes(b"JPEG_STUB")
        
        # Create symlink to real file
        symlink_file = photos_dir / "link_to_photo.jpg"
        symlink_file.symlink_to(real_file)
        
        # Create database and scanner
        db = Database(tmp_path / "test.sqlite3")
        thumbnailer = Thumbnailer()
        scanner = MediaScanner(db, thumbnailer)
        
        # Scan directory
        scanner.scan([("photos", "Photos", str(photos_dir))])
        
        # Verify: Only real file indexed, symlink skipped
        media = db.list_media("photos")
        assert len(media) == 1
        assert media[0].name == "photo.jpg"
        assert not any("link" in m.name for m in media)


def test_scanner_detects_symlink_directory():
    """Test: Scanner skips directories that are symlinks."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create real photos directory
        real_photos = tmp_path / "real_photos"
        real_photos.mkdir()
        real_photo = real_photos / "photo.jpg"
        real_photo.write_bytes(b"JPEG_STUB")
        
        # Create link to the photos directory
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        symlink_dir = scan_dir / "linked_photos"
        symlink_dir.symlink_to(real_photos)
        
        # Database and scanner
        db = Database(tmp_path / "test.sqlite3")
        thumbnailer = Thumbnailer()
        scanner = MediaScanner(db, thumbnailer)
        
        # Scan the scan_dir (which contains symlink to photos)
        scanner.scan([("photos", "Photos", str(scan_dir))])
        
        # Verify: No files indexed from symlink directory
        media = db.list_media("photos")
        assert len(media) == 0


def test_scanner_skips_broken_symlinks():
    """Test: Scanner handles broken symlinks gracefully."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create directory with broken symlink
        photos_dir = tmp_path / "photos"
        photos_dir.mkdir()
        
        # Create symlink to non-existent file
        broken_link = photos_dir / "broken.jpg"
        broken_link.symlink_to("/nonexistent/file.jpg")
        
        # Create a real file
        real_file = photos_dir / "real.jpg"
        real_file.write_bytes(b"JPEG_STUB")
        
        db = Database(tmp_path / "test.sqlite3")
        thumbnailer = Thumbnailer()
        scanner = MediaScanner(db, thumbnailer)
        
        # Scan should not crash on broken symlink
        scanner.scan([("photos", "Photos", str(photos_dir))])
        
        # Verify: Only real file indexed
        media = db.list_media("photos")
        assert len(media) == 1
        assert media[0].name == "real.jpg"


def test_scanner_handles_symlink_loop():
    """Test: Scanner doesn't infinite-loop on circular symlinks."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create two directories that link to each other (circular)
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        
        # Create a real file in dir_a
        photo = dir_a / "photo.jpg"
        photo.write_bytes(b"JPEG_STUB")
        
        # Create symlinks pointing to each other (potential loop)
        link_ab = dir_a / "link_to_b"
        link_ba = dir_b / "link_to_a"
        link_ab.symlink_to(dir_b)
        link_ba.symlink_to(dir_a)
        
        db = Database(tmp_path / "test.sqlite3")
        thumbnailer = Thumbnailer()
        scanner = MediaScanner(db, thumbnailer)
        
        # Scan should complete without infinite loop
        scanner.scan([("photos", "Photos", str(dir_a))])
        
        # Verify: Photo found, no duplicates from loop
        media = db.list_media("photos")
        assert len(media) == 1
        assert media[0].name == "photo.jpg"
