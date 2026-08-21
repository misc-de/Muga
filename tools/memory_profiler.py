#!/usr/bin/env python3
"""
Memory profiling script for Yaga.
Measures RAM usage under different loads.
"""
import tempfile
from pathlib import Path
import time
import tracemalloc
import sys

from yaga.database import Database
from yaga.scanner import MediaScanner
from yaga.thumbnails import Thumbnailer


def format_size(bytes_val: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def measure_operation(name: str, func, *args, **kwargs):
    """Measure memory usage of a function."""
    tracemalloc.reset_peak()
    tracemalloc.start()
    start_time = time.time()
    
    result = func(*args, **kwargs)
    
    elapsed = time.time() - start_time
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"\n📊 {name}")
    print(f"   Time: {elapsed:.2f}s | Current: {format_size(current)} | Peak: {format_size(peak)}")
    return result


def test_database_memory():
    """Test: Load 50k files into database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        def load_many_files():
            # Simulate loading 50k files
            for i in range(50000):
                tmp_path / f"photo_{i:06d}.jpg"
                # Don't create actual files, just simulate DB inserts
                # (Database.upsert_media doesn't actually need stat() for path validation here)
                db.conn.execute(
                    """
                    INSERT INTO media(path, category, media_type, folder, name, mtime, size, thumb_path, seen_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path, category) DO UPDATE SET
                        media_type=excluded.media_type,
                        folder=excluded.folder,
                        name=excluded.name,
                        mtime=excluded.mtime,
                        size=excluded.size,
                        thumb_path=COALESCE(excluded.thumb_path, media.thumb_path),
                        seen_at=excluded.seen_at
                    """,
                    (f"/photos/{i:06d}.jpg", "local", "image", "/", f"photo_{i:06d}.jpg", 1000000000 + i, 1024000, None, time.time()),
                )
                if i % 5000 == 0:
                    db.conn.commit()
            db.conn.commit()
        
        measure_operation("Database: Insert 50k files", load_many_files)
        
        # Query all files
        def query_all():
            db.list_media("local")
        
        measure_operation("Database: Query all 50k files", query_all)


def test_scanner_memory():
    """Test: Scan folder with many subfolders."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        # Create nested folder structure
        photos_dir = tmp_path / "photos"
        photos_dir.mkdir()
        
        # Create 100 folders with 100 images each = 10k images
        for folder_idx in range(100):
            folder = photos_dir / f"folder_{folder_idx:03d}"
            folder.mkdir()
            for img_idx in range(100):
                img_path = folder / f"photo_{img_idx:03d}.jpg"
                img_path.write_bytes(b"JPEG_STUB")  # Minimal JPEG placeholder
        
        def scan_folder():
            scanner = MediaScanner(db)
            scanner.scan(photos_dir, "local")
        
        measure_operation("Scanner: Scan 10k images in 100 folders", scan_folder)
        
        # Verify files were indexed
        media_count = len(db.list_media("local"))
        print(f"   Indexed: {media_count} files")


def test_emoji_cache_memory():
    """Test: Build emoji sticker cache."""
    from yaga.editor import _EMOJI_PIL_CACHE, _emoji_to_pil
    
    def build_emoji_cache():
        _EMOJI_PIL_CACHE.clear()
        # Pre-render common emoji for stickers
        emoji_list = [
            "😀", "😁", "😂", "😃", "😄", "😅", "😆", "😇", "😈", "😉",
            "😊", "😋", "😌", "😍", "😎", "😏", "😐", "😑", "😒", "😓",
            "😔", "😕", "😖", "😗", "😘", "😙", "😚", "😛", "😜", "😝",
            "😞", "😟", "😠", "😡", "😢", "😣", "😤", "😥", "😦", "😧",
            "😨", "😩", "😪", "😫", "😬", "😭", "😮", "😯", "😰", "😱",
        ]
        for emoji in emoji_list:
            _emoji_to_pil(emoji, 128)
    
    measure_operation("Editor: Build emoji cache (50 emoji at 128px)", build_emoji_cache)
    
    print(f"   Emoji cache size: {len(_EMOJI_PIL_CACHE)} entries")


def test_thumbnail_cache_memory():
    """Test: Generate and cache thumbnails."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        thumbnailer = Thumbnailer()
        
        # Create test JPEG files
        test_images = []
        for i in range(100):
            test_path = tmp_path / f"photo_{i:03d}.jpg"
            # Create minimal JPEG (just a marker for now)
            test_path.write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 1000)  # JPEG header + padding
            test_images.append(test_path)
        
        def generate_thumbnails():
            for img_path in test_images:
                thumbnailer.ensure_thumbnail(img_path, "image")
        
        measure_operation("Thumbnails: Generate 100 thumbnails (320x320)", generate_thumbnails)


def test_gridview_rendering_memory():
    """Test: Simulate GridView rendering with many tiles."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = Database(tmp_path / "test.sqlite3")
        
        # Insert 1000 files into DB
        for i in range(1000):
            db.conn.execute(
                """
                INSERT INTO media(path, category, media_type, folder, name, mtime, size, thumb_path, seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"/photos/photo_{i:04d}.jpg", "local", "image", "/", f"photo_{i:04d}.jpg", 1000000000, 1024000, None, time.time()),
            )
        db.conn.commit()
        
        def render_grid():
            # Simulate loading grid tiles (without GTK widgets)
            media_list = db.list_media("local", sort_mode="newest", folder=None)
            # Each tile would hold: image reference, thumbnail, metadata
            tiles = []
            for item in media_list:
                tile = {
                    "name": item.name,
                    "path": item.path,
                    "thumb": item.thumb_path,
                    "size": item.size,
                    "mtime": item.mtime,
                }
                tiles.append(tile)
            return tiles
        
        measure_operation("GridView: Render 1000 tiles metadata", render_grid)


if __name__ == "__main__":
    print("🔍 Yaga Memory Profiling\n" + "=" * 50)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        # Quick tests for fast iteration
        test_database_memory()
        test_emoji_cache_memory()
    else:
        # Full suite
        print("\n⚙️ Running full memory profiling suite...\n")
        test_database_memory()
        test_scanner_memory()
        test_emoji_cache_memory()
        test_thumbnail_cache_memory()
        test_gridview_rendering_memory()
        
        print("\n" + "=" * 50)
        print("✅ Memory profiling complete")
        print("\nRecommendations:")
        print("  - Monitor peak memory during scrolling (GridView rendering)")
        print("  - Consider lazy-loading thumbnails (not all at once)")
        print("  - Emoji cache is expected ~50MB for 1000+ stickers")
        print("  - Database peak scales with number of files indexed")
