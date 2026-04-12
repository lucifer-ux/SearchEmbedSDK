#!/usr/bin/env python3
"""
Diagnostic script to test video indexing and frame extraction
"""

import sys
import os
import subprocess
from pathlib import Path

# Add to path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# Test video configuration - can be overridden with environment variable
DEFAULT_TEST_VIDEO = r"C:\Users\USER\Documents\test\Screen Recording 2026-03-28 220347.mp4"
TEST_VIDEO_PATH = os.environ.get("CONTEXTCORE_TEST_VIDEO", DEFAULT_TEST_VIDEO)

# Usage: Set CONTEXTCORE_TEST_VIDEO environment variable to use a different test video
# Example: export CONTEXTCORE_TEST_VIDEO="/path/to/your/test/video.mp4"

def test_ffmpeg():
    """Test if ffmpeg is available"""
    print("=" * 70)
    print("TEST 1: Check ffmpeg availability")
    print("=" * 70)
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✓ ffmpeg is available")
            print(result.stdout.split('\n')[0])
            return True
        else:
            print("✗ ffmpeg failed")
            print(result.stderr)
            return False
    except Exception as e:
        print(f"✗ ffmpeg not found: {e}")
        return False

def test_ffprobe():
    """Test if ffprobe is available"""
    print("\n" + "=" * 70)
    print("TEST 2: Check ffprobe availability")
    print("=" * 70)
    try:
        result = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✓ ffprobe is available")
            print(result.stdout.split('\n')[0])
            return True
        else:
            print("✗ ffprobe failed")
            print(result.stderr)
            return False
    except Exception as e:
        print(f"✗ ffprobe not found: {e}")
        return False

def test_video_file():
    """Test if video file exists and is readable"""
    print("\n" + "=" * 70)
    print("TEST 3: Check video file")
    print("=" * 70)
    video_path = Path(TEST_VIDEO_PATH)
    if video_path.exists():
        size_mb = video_path.stat().st_size / (1024 * 1024)
        print(f"✓ Video file exists: {video_path}")
        print(f"  Size: {size_mb:.2f} MB")
        return True
    else:
        print(f"✗ Video file not found: {video_path}")
        print(f"  Set CONTEXTCORE_TEST_VIDEO environment variable to specify a test video")
        return False

def test_video_duration():
    """Get video duration using ffprobe"""
    print("\n" + "=" * 70)
    print("TEST 4: Get video duration")
    print("=" * 70)
    video_path = Path(TEST_VIDEO_PATH)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        duration = float(result.stdout.strip())
        print(f"✓ Video duration: {duration:.2f} seconds ({duration/60:.2f} minutes)")
        return duration
    except Exception as e:
        print(f"✗ Failed to get duration: {e}")
        return None

def test_frame_extraction_scene():
    """Test scene-based frame extraction"""
    print("\n" + "=" * 70)
    print("TEST 5: Test scene-based frame extraction")
    print("=" * 70)
    import tempfile
    video_path = Path(TEST_VIDEO_PATH)
    tmpdir = Path(tempfile.mkdtemp(prefix="video_frames_test_"))
    
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video_path),
            "-vf", "select='gt(scene,0.4)',scale=640:-1",
            "-vsync", "vfr",
            str(tmpdir / "frame_%06d.jpg"),
        ]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        frames = list(tmpdir.glob("frame_*.jpg"))
        if frames:
            print(f"✓ Scene-based extraction successful: {len(frames)} frames extracted")
            for i, frame in enumerate(sorted(frames)[:3]):
                print(f"  - {frame.name}")
            if len(frames) > 3:
                print(f"  ... and {len(frames) - 3} more")
            return len(frames)
        else:
            print(f"✗ No frames extracted by scene detection")
            if result.stderr:
                print(f"  stderr: {result.stderr}")
            return 0
    except Exception as e:
        print(f"✗ Scene extraction failed: {e}")
        return 0
    finally:
        # Cleanup
        for frame in tmpdir.glob("frame_*.jpg"):
            try:
                frame.unlink()
            except:
                pass
        try:
            tmpdir.rmdir()
        except:
            pass

def test_frame_extraction_sample():
    """Test sample-based frame extraction"""
    print("\n" + "=" * 70)
    print("TEST 6: Test sample-based frame extraction (fallback)")
    print("=" * 70)
    import tempfile
    video_path = Path(TEST_VIDEO_PATH)
    tmpdir = Path(tempfile.mkdtemp(prefix="video_frames_sample_"))
    
    try:
        # Get duration first
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        duration = float(result.stdout.strip())
        max_frames = 80
        step = max(1.0, duration / max_frames)
        timestamps = [i * step for i in range(int(min(max_frames, __import__('math').ceil(duration / step))))]
        
        print(f"Video duration: {duration:.2f}s, will extract {len(timestamps)} frames at step {step:.2f}s")
        
        extracted = 0
        for idx, ts in enumerate(timestamps):
            outp = tmpdir / f"sample_{idx:06d}.jpg"
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-ss", str(ts),
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", "scale=640:-1",
                str(outp),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if outp.exists():
                    extracted += 1
                    if idx < 3:
                        print(f"  ✓ Frame at {ts:.2f}s extracted")
            except Exception as e:
                print(f"  ✗ Failed to extract frame at {ts:.2f}s: {e}")
                break
        
        print(f"✓ Sample-based extraction: {extracted}/{len(timestamps)} frames extracted")
        return extracted
    except Exception as e:
        print(f"✗ Sample extraction failed: {e}")
        return 0
    finally:
        # Cleanup
        for frame in tmpdir.glob("sample_*.jpg"):
            try:
                frame.unlink()
            except:
                pass
        try:
            tmpdir.rmdir()
        except:
            pass

def test_video_index_pipeline():
    """Test the full video indexing pipeline"""
    print("\n" + "=" * 70)
    print("TEST 7: Test full video indexing pipeline")
    print("=" * 70)
    try:
        from run_index_pipeline import IndexPipeline
        from pathlib import Path
        
        pipeline = IndexPipeline()
        video_path = Path(TEST_VIDEO_PATH)
        
        print(f"Testing video file: {video_path}")
        result = pipeline.index_video_file(video_path)
        print(f"✓ Pipeline result: {result}")
        return result
    except Exception as e:
        print(f"✗ Pipeline test failed: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("VIDEO INDEXING DIAGNOSTIC TEST")
    print("=" * 70)
    
    # Run all tests
    ffmpeg_ok = test_ffmpeg()
    ffprobe_ok = test_ffprobe()
    file_ok = test_video_file()
    duration = test_video_duration()
    scene_frames = test_frame_extraction_scene() if (ffmpeg_ok and file_ok) else 0
    sample_frames = test_frame_extraction_sample() if (ffmpeg_ok and file_ok) else 0
    pipeline_result = test_video_index_pipeline() if (ffmpeg_ok and file_ok) else None
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"ffmpeg available:      {' ✓' if ffmpeg_ok else ' ✗'}")
    print(f"ffprobe available:     {' ✓' if ffprobe_ok else ' ✗'}")
    print(f"Video file exists:     {' ✓' if file_ok else ' ✗'}")
    print(f"Video duration:        {f'{duration:.2f}s' if duration else 'N/A'}")
    print(f"Scene frames extracted:{f' {scene_frames}' if scene_frames > 0 else ' 0 (fallback to sampling)'}")
    print(f"Sample frames extracted:{f' {sample_frames}' if sample_frames > 0 else ' 0 (failed)'}")
    print(f"Pipeline result:       {pipeline_result if pipeline_result else 'N/A'}")
    print("=" * 70)
