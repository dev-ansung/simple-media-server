#!/usr/bin/env python3
# /// script
# dependencies = ["Pillow", "tqdm"]
# ///
"""Serve a media directory with Caddy and print the local network URL."""

import argparse
import http.server
import json
import math
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

# Try to import sprite generator functions
try:
    from sprite_generator import probe_video, capture_frame, assemble_sprite
except ImportError:
    try:
        from .sprite_generator import probe_video, capture_frame, assemble_sprite
    except ImportError:
        # Fallback definitions in case sprite_generator module is missing or cannot be imported
        def probe_video(video_path: str) -> tuple[float, int, int]:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_entries", "format=duration:stream=width,height,codec_type",
                    "--", video_path,
                ],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            video_stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
            return duration, video_stream["width"], video_stream["height"]

        def capture_frame(video_path: str, timestamp: float, width: int, height: int, out_path: str):
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner",
                    "-ss", str(timestamp),
                    "-i", video_path,
                    "-frames:v", "1",
                    "-vf", f"scale={width}:{height}",
                    "-q:v", "5",
                    out_path,
                ],
                capture_output=True, check=True,
            )

        def assemble_sprite(frame_paths: list[str], output_path: str, width: int, height: int, columns: int):
            from PIL import Image
            n = len(frame_paths)
            rows = math.ceil(n / columns)
            sprite = Image.new("RGB", (width * columns, height * rows))
            for i, path in enumerate(frame_paths):
                x = (i % columns) * width
                y = (i // columns) * height
                sprite.paste(Image.open(path), (x, y))
            sprite.save(output_path, quality=90)


def get_local_ips() -> list[str]:
    """Get all non-loopback network IPs, preferring 192.168.x.x or 10.x.x.x ranges."""
    try:
        interfaces = socket.getaddrinfo(socket.gethostname(), None)
        ips = []
        for interface in interfaces:
            ip = interface[4][0]
            # Ignore loopback and IPv6 (which contain colons) for cleaner command-line display
            if ip != '127.0.0.1' and ':' not in ip:
                if ip not in ips:
                    ips.append(ip)
        
        # Sort so that 192.168.x.x or 10.x.x.x are preferred/first
        ips.sort(key=lambda ip: 0 if ip.startswith(('192.168.', '10.')) else 1)
        
        return ips if ips else ["127.0.0.1"]
    except Exception:
        return ["127.0.0.1"]


def check_dependencies():
    """Verify caddy and ffmpeg are installed and accessible on the PATH."""
    caddy_installed = False
    if shutil.which("caddy"):
        caddy_installed = True
    else:
        try:
            subprocess.run(["caddy", "version"], capture_output=True, check=True)
            caddy_installed = True
        except Exception:
            pass

    if not caddy_installed:
        print("Error: 'caddy' is not installed or not in your PATH.", file=sys.stderr)
        print("Please install Caddy server before running this media server:", file=sys.stderr)
        print("  - macOS: brew install caddy", file=sys.stderr)
        print("  - Linux: sudo apt install caddy (Debian/Ubuntu) or equivalent", file=sys.stderr)
        print("  - Windows: choco install caddy or download from https://caddyserver.com", file=sys.stderr)
        sys.exit(1)

    # Check ffmpeg and ffprobe
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not shutil.which("ffprobe"):
        missing.append("ffprobe")
    if missing:
        print(f"Warning: {', '.join(missing)} not found in PATH.", file=sys.stderr)
        print("On-demand sprite generation will fail without FFmpeg & FFprobe.", file=sys.stderr)
        print("Please install them using your package manager (e.g. 'brew install ffmpeg', 'sudo apt install ffmpeg', or 'choco install ffmpeg').\n", file=sys.stderr)


def generate_video_sprites(video_path: Path, output_sprite_path: Path, num_frames=30, width=160, columns=10):
    """Generate 30 frames from video in parallel, stitch them and save configuration."""
    from concurrent.futures import ThreadPoolExecutor
    import time
    
    t0 = time.perf_counter()
    
    # 1. Probe video metadata
    duration, src_width, src_height = probe_video(str(video_path))
    height = round(width * src_height / src_width)
    interval = duration / num_frames
    timestamps = [i * interval for i in range(num_frames)]
    
    config_path = output_sprite_path.with_suffix(".json")
    
    sys.stderr.write(f"[Python Helper]   - Target Video:  {video_path.name} ({duration:.1f}s, {src_width}x{src_height})\n")
    sys.stderr.write(f"[Python Helper]   - Config:        {num_frames} frames @ {width}x{height} each (every {interval:.1f}s)\n")
    sys.stderr.write(f"[Python Helper]   - Output Image:  {output_sprite_path.name}\n")
    sys.stderr.write(f"[Python Helper]   - Output Config: {config_path.name}\n")
    
    # 2. Extract frames to temporary directory in parallel
    with tempfile.TemporaryDirectory() as tmpdir:
        frame_paths = [str(Path(tmpdir) / f"frame_{i:04d}.jpg") for i in range(num_frames)]
        
        workers = min(4, num_frames)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(capture_frame, str(video_path), ts, width, height, path)
                for ts, path in zip(timestamps, frame_paths)
            ]
            for future in futures:
                future.result()  # Propagates exceptions
                
        # 3. Assemble and write the sprite sheet image
        assemble_sprite(frame_paths, str(output_sprite_path), width=width, height=height, columns=columns)
        
    # 4. Write the corresponding plugin config JSON file
    rows = math.ceil(num_frames / columns)
    config = {
        "url": output_sprite_path.name,
        "width": width,
        "height": height,
        "columns": columns,
        "rows": rows,
        "interval": math.floor(interval),
    }
    
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        
    elapsed = time.perf_counter() - t0
    sys.stderr.write(f"[Python Helper]   - Success:       Generated sprites for {video_path.name} in {elapsed:.2f}s\n")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    media_root = None
    generation_lock = None


class SpriteRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Log to stderr cleanly
        sys.stderr.write(f"[Python Helper] {format % args}\n")

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path_str = urllib.parse.unquote(parsed_url.path)
        
        media_root = self.server.media_root
        
        # Check if the request is to force generate/regenerate sprites
        if path_str.startswith("/_generate/"):
            video_rel_path = path_str.replace("/_generate/", "", 1)
            try:
                video_path = (media_root / video_rel_path.lstrip("/")).resolve()
                if not video_path.is_relative_to(media_root):
                    self.send_error(403, "Forbidden")
                    return
            except Exception:
                self.send_error(400, "Bad Request")
                return

            if not video_path.exists():
                self.send_error(404, "Video file not found")
                return

            sprite_jpg_path = video_path.with_name(f"{video_path.stem}.sprites.jpg")
            sprite_json_path = video_path.with_name(f"{video_path.stem}.sprites.json")

            try:
                with self.server.generation_lock:
                    self.log_message("Force generating sprites for %s...", video_path.name)
                    generate_video_sprites(video_path, sprite_jpg_path)
                
                # Return the generated JSON configuration
                data = sprite_json_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.log_message("Error force generating sprites: %s", str(e))
                self.send_error(500, f"Sprite generation failed: {e}")
            return

        # Security check: prevent directory traversal
        try:
            resolved_path = (media_root / path_str.lstrip("/")).resolve()
            if not resolved_path.is_relative_to(media_root):
                self.send_error(403, "Forbidden")
                return
        except Exception:
            self.send_error(400, "Bad Request")
            return

        # Check if the requested file is a sprite configuration or image
        if not (resolved_path.name.endswith(".sprites.json") or resolved_path.name.endswith(".sprites.jpg")):
            self.send_error(404, "Not Found")
            return

        # Map to actual sprite file names
        if resolved_path.name.endswith(".sprites.json"):
            sprite_json_path = resolved_path
            sprite_jpg_path = resolved_path.with_suffix(".jpg")
        else:
            sprite_jpg_path = resolved_path
            sprite_json_path = resolved_path.with_suffix(".json")

        # Generate files if they do not exist
        if not sprite_json_path.exists() or not sprite_jpg_path.exists():
            base_name = resolved_path.name.rsplit(".sprites.", 1)[0]
            video_dir = resolved_path.parent
            video_exts = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v', 'ts', 'flv']
            video_path = None
            for ext in video_exts:
                candidate = video_dir / f"{base_name}.{ext}"
                if candidate.exists():
                    video_path = candidate
                    break

            if not video_path:
                self.send_error(404, "Associated video file not found")
                return

            try:
                # Use thread-safe generation lock to avoid concurrent processes generating the same sprite
                with self.server.generation_lock:
                    if not sprite_json_path.exists() or not sprite_jpg_path.exists():
                        self.log_message("Generating sprites for %s...", video_path.name)
                        generate_video_sprites(video_path, sprite_jpg_path)
            except Exception as e:
                self.log_message("Error generating sprites: %s", str(e))
                self.send_error(500, f"Sprite generation failed: {e}")
                return

        # Serve the generated file
        try:
            content_type = "application/json" if resolved_path.name.endswith(".json") else "image/jpeg"
            data = resolved_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Failed serving file: {e}")


def start_helper_server(media_root: Path) -> tuple[ThreadedHTTPServer, int]:
    server = ThreadedHTTPServer(("127.0.0.1", 0), SpriteRequestHandler)
    server.media_root = media_root.resolve()
    server.generation_lock = threading.Lock()
    port = server.server_address[1]
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def main():
    parser = argparse.ArgumentParser(description="Serve a media directory with Caddy and on-demand sprite generation.")
    parser.add_argument("directory", nargs="?", default=None, help="Media directory to serve (default: ./videos or current directory)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()

    # Verify dependencies upfront
    check_dependencies()

    app_dir = Path(__file__).parent.resolve()
    if args.directory is None:
        candidate = app_dir / "videos"
        directory = candidate if candidate.exists() else Path(".").resolve()
    else:
        directory = Path(args.directory).resolve()

    if not directory.exists():
        print(f"Error: directory not found: {directory}", file=sys.stderr)
        sys.exit(1)

    # Start the Python helper server in background
    helper_server, helper_port = start_helper_server(directory)

    local_ips = get_local_ips()
    print(f"Serving {directory}")
    print()
    print(f"  Local:   http://localhost:{args.port}")
    for ip in local_ips:
        print(f"  Network: http://{ip}:{args.port}")
    print()
    print("Press Ctrl+C to stop.")

    # Convert paths to Posix/forward slash paths for Caddyfile cross-platform compatibility
    directory_posix = directory.as_posix()
    app_dir_posix = app_dir.as_posix()

    caddyfile = f"""{{
    admin off
}}

:{args.port} {{
    # Reverse proxy missing sprites to python helper
    @missing_sprites {{
        path *.sprites.json *.sprites.jpg
        not file {{
            root "{directory_posix}"
            try_files {{path}}
        }}
    }}
    handle @missing_sprites {{
        reverse_proxy 127.0.0.1:{helper_port}
    }}

    # Route /_generate/* directly to Python helper
    handle /_generate/* {{
        reverse_proxy 127.0.0.1:{helper_port}
    }}

    # /_ls/* — browse/JSON API, strips prefix and serves from media root
    handle /_ls/* {{
        uri strip_prefix /_ls
        file_server {{
            root "{directory_posix}"
            browse
        }}
    }}

    # / and /app.html — serve the UI shell
    handle /app.html {{
        file_server {{
            root "{app_dir_posix}"
        }}
    }}
    handle / {{
        rewrite * /app.html
        file_server {{
            root "{app_dir_posix}"
        }}
    }}

    # Everything else — serve media files directly
    handle {{
        file_server {{
            root "{directory_posix}"
        }}
    }}
}}
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.caddyfile', delete=False) as f:
        f.write(caddyfile)
        caddyfile_path = f.name

    try:
        # Determine absolute path to caddy for robustness
        caddy_bin = shutil.which("caddy") or "caddy"
        subprocess.run(
            [caddy_bin, "run", "--config", caddyfile_path, "--adapter", "caddyfile"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(f"\nError: Caddy failed to start. Is another process already listening on port {args.port}?", file=sys.stderr)
        print(f"Please check with 'lsof -i :{args.port}' or try a different port with '--port <PORT>'.\n", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopping media server...")
    except FileNotFoundError:
        print("Error: 'caddy' not found. Install it from https://caddyserver.com/docs/install", file=sys.stderr)
        sys.exit(1)
    finally:
        helper_server.shutdown()
        helper_server.server_close()
        try:
            os.unlink(caddyfile_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
