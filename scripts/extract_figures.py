#!/usr/bin/env python3
"""Download figure images from parsed article data."""

import sys
import time
from pathlib import Path

import requests
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def download_figures(figures: list, study_id: str, output_dir: Path = None) -> list[dict]:
    """Download figure images and return updated figure metadata.

    Args:
        figures: List of ParsedFigure objects or dicts with 'image_url' and 'caption'
        study_id: Paper identifier for organizing files
        output_dir: Base figures directory (default: data/figures/)

    Returns:
        List of dicts with local_path added
    """
    config = load_config()
    if output_dir is None:
        output_dir = ROOT_DIR / config["paths"]["figures_dir"]

    paper_dir = output_dir / study_id
    paper_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for fig in figures:
        fig_url = fig.image_url if hasattr(fig, 'image_url') else fig.get("image_url", "")
        fig_id = fig.figure_id if hasattr(fig, 'figure_id') else fig.get("figure_id", "unknown")
        caption = fig.caption if hasattr(fig, 'caption') else fig.get("caption", "")

        if not fig_url:
            print(f"  ⚠ No URL for {fig_id}, skipping")
            results.append({
                "figure_id": fig_id,
                "caption": caption,
                "image_url": fig_url,
                "local_path": None,
                "status": "no_url",
            })
            continue

        # Determine file extension
        ext = _get_extension(fig_url)
        local_filename = f"{fig_id}{ext}"
        local_path = paper_dir / local_filename

        if local_path.exists():
            print(f"  Already downloaded: {fig_id}")
            results.append({
                "figure_id": fig_id,
                "caption": caption,
                "image_url": fig_url,
                "local_path": str(local_path),
                "status": "exists",
            })
            continue

        try:
            print(f"  Downloading {fig_id}...")
            _download_image(fig_url, local_path)

            # Convert SVG to PNG if needed
            if ext == ".svg" and config.get("figures", {}).get("convert_svg_to_png", True):
                png_path = local_path.with_suffix(".png")
                _svg_to_png(local_path, png_path)
                local_path = png_path

            results.append({
                "figure_id": fig_id,
                "caption": caption,
                "image_url": fig_url,
                "local_path": str(local_path),
                "status": "downloaded",
            })

        except Exception as e:
            print(f"  ✗ Failed to download {fig_id}: {e}")
            results.append({
                "figure_id": fig_id,
                "caption": caption,
                "image_url": fig_url,
                "local_path": None,
                "status": f"error: {e}",
            })

        # Rate limiting
        time.sleep(0.5)

    downloaded = sum(1 for r in results if r["status"] in ("downloaded", "exists"))
    print(f"  📸 {downloaded}/{len(figures)} figures downloaded for {study_id}")

    return results


def _get_extension(url: str) -> str:
    """Determine file extension from URL."""
    url_lower = url.lower().split("?")[0]
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".tiff", ".tif", ".webp"]:
        if url_lower.endswith(ext):
            return ext
    return ".png"  # Default to PNG


def _download_image(url: str, output_path: Path):
    """Download an image from URL to local path."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SensoryExtraction/1.0)",
    }
    resp = requests.get(url, headers=headers, timeout=30, stream=True)
    resp.raise_for_status()

    # Check file size
    content_length = resp.headers.get("content-length")
    if content_length and int(content_length) > 10 * 1024 * 1024:  # 10MB
        raise ValueError(f"Image too large: {int(content_length) / 1024 / 1024:.1f} MB")

    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def _svg_to_png(svg_path: Path, png_path: Path):
    """Convert SVG to PNG using Pillow/cairosvg (best effort)."""
    try:
        import cairosvg
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))
    except ImportError:
        print(f"  ⚠ cairosvg not installed, keeping SVG: {svg_path}")


def main():
    # CLI usage for testing: provide a URL and study_id
    if len(sys.argv) < 3:
        print("Usage: python extract_figures.py <study_id> <url1> [url2] ...")
        print("Example: python extract_figures.py wee2018 https://example.com/fig1.png")
        sys.exit(1)

    study_id = sys.argv[1]
    urls = sys.argv[2:]

    figures = [
        {"figure_id": f"figure_{i+1}", "image_url": url, "caption": ""}
        for i, url in enumerate(urls)
    ]

    results = download_figures(figures, study_id)
    for r in results:
        print(f"  {r['figure_id']}: {r['status']} → {r.get('local_path', 'N/A')}")


if __name__ == "__main__":
    main()
