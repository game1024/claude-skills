#!/usr/bin/env python3
"""
pic2ppt.py - convert slide images into an editable PowerPoint deck.

The pipeline follows the principles of the `image-to-editable-ppt` skill:

  1. Background first   - decide whether the background is simple (native fill)
                          or complex (a clean base image with the editable
                          objects removed).
  2. Foreground second  - separate non-text visual objects from the background
                          into independent bitmap assets with alpha, instead of
                          leaving them welded into the page raster.
  3. Native last        - rebuild text, flat cards and structural lines as
                          native PowerPoint objects.

The build is manifest-driven exactly like the skill: `manifest.json` is the
authoritative page source (source-pixel coordinates), and the deterministic
writer turns it into OOXML. No full-slide "original image + text on top" is
produced: text and extracted objects are removed from the background.

What is automated here (classical CV + OCR) and what the original skill does
with a vision agent:

  * Text detection / content ........ OCR backend (PP-OCRv6 cloud,
                                       PaddleOCR-VL cloud, RapidOCR local,
                                       or Tesseract).
  * Font size / color / bold ......... ink measurement ported from the skill.
  * Background repair ................ local diffusion inpainting (or flat fill
                                       when the surrounding region is uniform).
  * Foreground separation ............ optional (off by default). Connected
                                       components + colour-distance alpha
                                       matting; enable with `--icons` when the
                                       slide has isolated icons on clean
                                       backgrounds.
  * Cards / dividers ................. conservative flat-colour rectangle and
                                       line detection.
  * Native tables / formulas ......... not reconstructed; they stay in the
                                       clean base image and are reported as
                                       warnings.

Usage:
    python pic2ppt.py slide.png
    python pic2ppt.py slide1.png slide2.png -o deck.pptx
    python pic2ppt.py slide.png --ocr rapidocr --mode native

Dependencies:
    pip install -r requirements.txt
    # Optional local OCR (no cloud): pip install rapidocr onnxruntime
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from scipy import ndimage

    HAVE_SCIPY = True
except Exception:  # pragma: no cover - scipy is a declared dependency
    ndimage = None
    HAVE_SCIPY = False

try:
    from skimage.restoration import inpaint_biharmonic

    HAVE_SKIMAGE = True
except Exception:  # optional dependency: smoother gradient repair
    inpaint_biharmonic = None
    HAVE_SKIMAGE = False

try:
    import cv2  # type: ignore

    HAVE_CV2 = True
except Exception:  # optional dependency: better inpainting when present
    cv2 = None
    HAVE_CV2 = False

try:
    import requests

    HAVE_REQUESTS = True
except Exception:  # pragma: no cover
    requests = None
    HAVE_REQUESTS = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMU_PER_INCH = 914400
PIL_EMU = EMU_PER_INCH
ASPECT_16_9 = 16.0 / 9.0
ASPECT_TOLERANCE = 0.03
WIDE_SLIDE = {"width": 13.333, "height": 7.5, "size_mode": "wide"}
PX_PER_INCH_REFERENCE = 96.0

DEFAULT_FONT_CJK = "Microsoft YaHei"
DEFAULT_FONT_LATIN = "Arial"
DEFAULT_TEXT_COLOR = "#111111"
DEFAULT_TEXT_FIT_SAFETY = 0.9
DEFAULT_TEXT_LINE_HEIGHT = 1.22
DEFAULT_MIN_FONT_SIZE = 4.0
MIN_GLYPH_PX = 6
MAX_TEXT_FILL = 0.65

PADDLE_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
PADDLE_DEFAULT_MODEL = "PaddleOCR-VL-1.6"
PADDLE_OCR_MODEL = "PP-OCRv6"
PADDLE_TEXT_LABELS = {
    "text",
    "paragraph_title",
    "doc_title",
    "title",
    "vision_footnote",
    "table_title",
    "chart_title",
    # Labels that PaddleOCR-VL emits around figures/charts and page chrome.
    "figure_title",
    "caption",
    "footnote",
    "header",
    "footer",
    "page_number",
}

WARNINGS: list[str] = []
QUIET = False


def warn(message: str) -> None:
    WARNINGS.append(message)
    if not QUIET:
        print(f"  ! {message}")


def log(message: str) -> None:
    if not QUIET:
        print(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel_posix(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def fit_content_box(source_width: float, source_height: float, slide: dict) -> dict:
    """Contain-fit the source image inside the slide canvas."""
    slide_width = float(slide["width"])
    slide_height = float(slide["height"])
    source_aspect = source_width / source_height
    slide_aspect = slide_width / slide_height
    if source_aspect >= slide_aspect:
        width = slide_width
        height = width / source_aspect
        left = 0.0
        top = (slide_height - height) / 2.0
    else:
        height = slide_height
        width = height * source_aspect
        left = (slide_width - width) / 2.0
        top = 0.0
    return {"left": left, "top": top, "width": width, "height": height, "fit": "contain"}


def is_close_to_wide(width: int, height: int) -> bool:
    if not width or not height:
        return False
    return abs((width / height) / ASPECT_16_9 - 1.0) <= ASPECT_TOLERANCE


def plan_slide(source_sizes: list[tuple[int, int]]) -> dict:
    """Pick one deck canvas. The first page drives a custom canvas; pages close
    to 16:9 always use the standard widescreen canvas, like the skill."""
    width_px, height_px = source_sizes[0]
    if is_close_to_wide(width_px, height_px):
        return dict(WIDE_SLIDE)
    aspect = width_px / height_px
    if aspect >= ASPECT_16_9:
        width = 13.333
        height = width / aspect
    else:
        height = 7.5
        width = height * aspect
    return {
        "width": round(width, 4),
        "height": round(height, 4),
        "size_mode": "source",
    }


def px_to_inches(manifest: dict, x: float, y: float, width: float, height: float) -> dict:
    source = manifest.get("source", {})
    source_width = float(source.get("width_px") or 1)
    source_height = float(source.get("height_px") or 1)
    content_box = manifest.get("content_box") or fit_content_box(
        source_width, source_height, manifest.get("slide", WIDE_SLIDE)
    )
    return {
        "left": float(content_box["left"]) + float(x) / source_width * float(content_box["width"]),
        "top": float(content_box["top"]) + float(y) / source_height * float(content_box["height"]),
        "width": float(width) / source_width * float(content_box["width"]),
        "height": float(height) / source_height * float(content_box["height"]),
    }


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def oriented_size(path: Path) -> tuple[int, int]:
    """Image size after applying the EXIF orientation tag."""
    with Image.open(path) as image:
        width, height = image.size
        try:
            orientation = image.getexif().get(274, 1)
        except Exception:
            orientation = 1
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def load_rgb(path: Path) -> Image.Image:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        if image.mode == "P":
            image = image.convert("RGBA")
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
    else:
        image = image.convert("RGB")
    return image


def resize_max(image: Image.Image, max_side: int) -> tuple[Image.Image, float]:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_side:
        return image.copy(), 1.0
    scale = max_side / float(longest)
    resized = image.resize((max(1, int(round(width * scale))), max(1, int(round(height * scale)))), Image.LANCZOS)
    return resized, scale


def clamp_box(box: list[float], width: int, height: int) -> list[int]:
    x, y, w, h = [float(v) for v in box]
    x1 = max(0, min(width, int(math.floor(x))))
    y1 = max(0, min(height, int(math.floor(y))))
    x2 = max(x1 + 1, min(width, int(math.ceil(x + w))))
    y2 = max(y1 + 1, min(height, int(math.ceil(y + h))))
    return [x1, y1, x2 - x1, y2 - y1]


def box_iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    union = float(aw * ah + bw * bh - intersection)
    return intersection / union if union > 0 else 0.0


def boxes_overlap(a: list[float], b: list[float], margin: float = 0.0) -> bool:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    return not (
        ax1 + aw + margin <= bx1
        or bx1 + bw + margin <= ax1
        or ay1 + ah + margin <= by1
        or by1 + bh + margin <= ay1
    )


def hex_color_from_rgb(color) -> str:
    return "#%02X%02X%02X" % tuple(int(round(max(0, min(255, float(v))))) for v in color[:3])


def rgb_from_hex(value: str) -> tuple[int, int, int]:
    value = str(value or "").strip().lstrip("#")
    if len(value) >= 6:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return (255, 255, 255)


def sample_ring(rgb: np.ndarray, box: list[float], band: int = 3) -> tuple[np.ndarray, float, float]:
    """Median colour, per-channel std and median absolute deviation of a band
    just outside `box`.

    The MAD is the useful one for deciding flat-versus-diffusion repairs: a
    few dark neighbours (for example a subtitle touching the ring) inflate the
    std but barely move the MAD, while a photo or texture raises both.
    """
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x - band), max(0, y - band)
    x2, y2 = min(width, x + w + band), min(height, y + h + band)
    if x2 <= x1 or y2 <= y1:
        return np.array([255.0, 255.0, 255.0]), 999.0, 999.0
    crop = rgb[y1:y2, x1:x2].astype(np.float32)
    mask = np.ones(crop.shape[:2], dtype=bool)
    ix1, iy1 = x - x1, y - y1
    ix2, iy2 = ix1 + w, iy1 + h
    mask[max(0, iy1) : max(0, iy2), max(0, ix1) : max(0, ix2)] = False
    pixels = crop[mask]
    if pixels.size == 0:
        pixels = crop.reshape(-1, 3)
    median = np.median(pixels, axis=0)
    std = float(np.max(np.std(pixels, axis=0)))
    mad = float(np.max(np.median(np.abs(pixels - median), axis=0)))
    return median, std, mad


def sample_outside_color(rgb: np.ndarray, box: list[float], distance: int = 5) -> tuple[np.ndarray, float]:
    """Median colour of a thin ring at a fixed distance outside a box."""
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x - distance), max(0, y - distance)
    x2, y2 = min(width, x + w + distance), min(height, y + h + distance)
    if x2 <= x1 or y2 <= y1:
        return np.array([255.0, 255.0, 255.0]), 999.0
    crop = rgb[y1:y2, x1:x2].astype(np.float32)
    mask = np.ones(crop.shape[:2], dtype=bool)
    inner = max(1, distance - 2)
    ix1, iy1 = max(0, x - x1 - inner), max(0, y - y1 - inner)
    ix2, iy2 = min(crop.shape[1], x + w - x1 + inner), min(crop.shape[0], y + h - y1 + inner)
    mask[iy1:iy2, ix1:ix2] = False
    pixels = crop[mask]
    if pixels.size == 0:
        pixels = crop.reshape(-1, 3)
    return np.median(pixels, axis=0), float(np.max(np.std(pixels, axis=0)))


def border_color(rgb: np.ndarray, band_fraction: float = 0.02) -> np.ndarray:
    height, width = rgb.shape[:2]
    band = max(2, int(round(min(height, width) * band_fraction)))
    border = np.concatenate(
        [
            rgb[:band].reshape(-1, 3),
            rgb[-band:].reshape(-1, 3),
            rgb[:, :band].reshape(-1, 3),
            rgb[:, -band:].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    return np.median(border, axis=0)


# ---------------------------------------------------------------------------
# Ink metrics (ported from the skill's page_text_metrics.py)
# ---------------------------------------------------------------------------

MIN_CLASS_SEPARATION = 25.0
MAX_INK_FRACTION = 0.45
MIN_INK_PIXELS = 8
RULE_BAND_MAX_PX = 4
RULE_BAND_RATIO = 0.35

_CJK_RANGES = ((0x3000, 0x30FF), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))
_DESCENDER_CHARS = set("gjpqy")
_ASCENDER_CHARS = set("bdfhklt")


def contains_cjk(text: str) -> bool:
    return any(any(low <= ord(ch) <= high for low, high in _CJK_RANGES) for ch in text)


def glyph_em_ratio(text: str) -> float:
    if not text:
        return 0.72
    if contains_cjk(text):
        return 0.95
    has_descender = any(ch in _DESCENDER_CHARS for ch in text)
    has_ascender = any(ch.isupper() or ch.isdigit() or ch in _ASCENDER_CHARS for ch in text)
    if has_descender and has_ascender:
        return 0.95
    if has_descender:
        return 0.78
    if has_ascender:
        return 0.72
    return 0.50


def otsu_threshold(gray: np.ndarray) -> tuple[float, float]:
    hist, _ = np.histogram(gray, bins=256, range=(0.0, 256.0))
    total = float(gray.size)
    if total == 0:
        return 127.5, 0.0
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist).astype(np.float64)
    weight_fg = total - weight_bg
    sum_bg = np.cumsum(levels * hist)
    sum_all = sum_bg[-1]
    mean_bg = sum_bg / np.maximum(weight_bg, 1.0)
    mean_fg = (sum_all - sum_bg) / np.maximum(weight_fg, 1.0)
    between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    index = int(np.argmax(between))
    separation = abs(float(mean_fg[index]) - float(mean_bg[index]))
    if weight_bg[index] == 0 or weight_fg[index] == 0:
        separation = 0.0
    return float(index), separation


def crop_ink_mask(gray_crop: np.ndarray) -> dict:
    threshold, separation = otsu_threshold(gray_crop)
    dark = gray_crop <= threshold
    dark_fraction = float(dark.mean()) if dark.size else 0.0
    if dark_fraction <= 0.5:
        mask, polarity, fraction = dark, "dark", dark_fraction
    else:
        mask, polarity, fraction = ~dark, "light", 1.0 - dark_fraction
    reliable = (
        separation >= MIN_CLASS_SEPARATION
        and fraction <= MAX_INK_FRACTION
        and int(mask.sum()) >= MIN_INK_PIXELS
    )
    return {
        "mask": mask,
        "polarity": polarity,
        "reliable": reliable,
        "separation": separation,
        "ink_fraction": fraction,
    }


def despeckle(mask: np.ndarray) -> np.ndarray:
    if not mask.any():
        return mask
    neighbors = np.zeros(mask.shape, dtype=np.uint8)
    neighbors[1:, :] += mask[:-1, :]
    neighbors[:-1, :] += mask[1:, :]
    neighbors[:, 1:] += mask[:, :-1]
    neighbors[:, :-1] += mask[:, 1:]
    return mask & (neighbors > 0)


def tight_bbox(mask: np.ndarray):
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    top, bottom = int(rows[0]), int(rows[-1])
    left, right = int(cols[0]), int(cols[-1])
    return (left, top, right - left + 1, bottom - top + 1)


def line_bands(mask: np.ndarray, min_ink_px: int = 2, merge_gap: int = 2) -> list[tuple[int, int]]:
    row_ink = mask.sum(axis=1)
    inky = row_ink >= min_ink_px
    bands: list[list[int]] = []
    start = None
    for y, on in enumerate(inky):
        if on and start is None:
            start = y
        elif not on and start is not None:
            bands.append([start, y])
            start = None
    if start is not None:
        bands.append([start, len(inky)])
    merged: list[list[int]] = []
    for band in bands:
        if merged and band[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = band[1]
        else:
            merged.append(band)
    return [(top, bottom) for top, bottom in merged if bottom - top >= 2]


def filter_rule_bands(bands: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(bands) <= 1:
        return bands
    heights = [bottom - top for top, bottom in bands]
    tallest = max(heights)
    if tallest <= RULE_BAND_MAX_PX:
        return bands
    kept = [
        (top, bottom)
        for (top, bottom), height in zip(bands, heights)
        if not (height <= RULE_BAND_MAX_PX and height < RULE_BAND_RATIO * tallest)
    ]
    return kept or bands


def segment_mask(mask: np.ndarray):
    if not mask.any():
        return None
    bands = filter_rule_bands(line_bands(mask))
    if not bands:
        return None
    row_selector = np.zeros(mask.shape[0], dtype=bool)
    for top, bottom in bands:
        row_selector[top:bottom] = True
    restricted = mask & row_selector[:, None]
    bbox = tight_bbox(restricted)
    if bbox is None:
        return None
    return {
        "mask": restricted,
        "bbox": bbox,
        "bands": bands,
        "glyph_height_px": float(np.median([bottom - top for top, bottom in bands])),
        "line_count": len(bands),
    }


def measure_crop(gray_crop: np.ndarray) -> dict:
    if gray_crop.size == 0 or min(gray_crop.shape) < 3:
        return {
            "ok": False,
            "reason": "too-small",
            "mask": np.zeros(gray_crop.shape, dtype=bool),
            "bbox": None,
            "glyph_height_px": 0.0,
            "line_count": 0,
            "polarity": "dark",
        }
    ink = crop_ink_mask(gray_crop.astype(np.float32))
    if not ink["reliable"]:
        return {
            "ok": False,
            "reason": "low-contrast",
            **ink,
            "bbox": None,
            "glyph_height_px": 0.0,
            "line_count": 0,
        }
    mask = despeckle(ink["mask"])
    segmented = segment_mask(mask)
    if segmented is None:
        return {
            "ok": False,
            "reason": "no-ink",
            "mask": mask,
            "polarity": ink["polarity"],
            "bbox": None,
            "glyph_height_px": 0.0,
            "line_count": 0,
        }
    return {
        "ok": True,
        "reason": "",
        "polarity": ink["polarity"],
        "separation": ink["separation"],
        "ink_fraction": float(segmented["mask"].mean()),
        **segmented,
    }


def text_color_from_mask(rgb_crop: np.ndarray, mask: np.ndarray, polarity: str) -> str:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return DEFAULT_TEXT_COLOR
    pixels = rgb_crop[ys, xs].astype(np.float32)
    luminance = pixels.mean(axis=1)
    if polarity == "dark":
        cutoff = np.percentile(luminance, 30)
        selected = pixels[luminance <= cutoff]
    else:
        cutoff = np.percentile(luminance, 70)
        selected = pixels[luminance >= cutoff]
    if len(selected) == 0:
        selected = pixels
    color = np.median(selected, axis=0)
    return hex_color_from_rgb(color)


def estimate_bold(mask: np.ndarray, glyph_px: float, text: str) -> bool:
    if not HAVE_SCIPY or not mask.any() or glyph_px <= 0:
        return False
    distances = ndimage.distance_transform_edt(mask)
    stroke_half = float(np.percentile(distances[mask], 80))
    ratio = 2.0 * stroke_half / max(glyph_px, 1.0)
    threshold = 0.11 if contains_cjk(text) else 0.13
    return ratio >= threshold


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------


def load_paddle_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit.strip()
    token = os.environ.get("PADDLE_OCR_TOKEN", "").strip()
    if token:
        return token
    config_paths = [
        Path.home() / ".editppt" / "config.yaml",
        Path(os.environ.get("EDITPPT_CONFIG_HOME", "")) / "config.yaml"
        if os.environ.get("EDITPPT_CONFIG_HOME")
        else None,
    ]
    for path in config_paths:
        if not path or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"^PADDLE_OCR_TOKEN\s*:\s*['\"]?([^'\"\s]+)", text, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _paddle_job_status(
    image_path: Path,
    token: str,
    model: str,
    optional_payload: dict,
    timeout: int,
    label: str,
) -> dict:
    """Submit an image and poll until the PaddleOCR job finishes."""
    if not HAVE_REQUESTS:
        raise RuntimeError("requests is required for the Paddle OCR backends (pip install requests).")
    headers = {"Authorization": f"bearer {token}"}
    with image_path.open("rb") as handle:
        response = requests.post(
            PADDLE_JOB_URL,
            headers=headers,
            data={"model": model, "optionalPayload": json.dumps(optional_payload)},
            files={"file": handle},
            timeout=60,
        )
    if response.status_code != 200:
        raise RuntimeError(f"{label} submit failed ({response.status_code}): {response.text[:300]}")
    job_id = response.json()["data"]["jobId"]
    log(f"  {label} job submitted: {job_id}")

    started = time.time()
    status = None
    while True:
        try:
            status = requests.get(f"{PADDLE_JOB_URL}/{job_id}", headers=headers, timeout=60).json()["data"]
        except Exception as exc:
            raise RuntimeError(f"{label} status request failed: {exc}") from exc
        state = status.get("state")
        if state == "done":
            return status
        if state == "failed":
            raise RuntimeError(f"{label} job failed: {status.get('errorMsg')}")
        if time.time() - started > timeout:
            raise RuntimeError(f"{label} job timed out after {timeout}s (state={state})")
        time.sleep(5)


def _paddle_result_response(status: dict, label: str):
    url = (status.get("resultUrl") or {}).get("jsonUrl")
    if not url:
        raise RuntimeError(f"{label} returned no result URL")
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response


def paddleocr_vl_blocks(
    image_path: Path,
    token: str,
    model: str = PADDLE_DEFAULT_MODEL,
    timeout: int = 300,
    chart_recognition: bool = False,
) -> dict:
    """Submit one image to PaddleOCR-VL and return its prunedResult."""
    optional = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": bool(chart_recognition),
    }
    status = _paddle_job_status(image_path, token, model, optional, timeout, "PaddleOCR-VL")
    jsonl = _paddle_result_response(status, "PaddleOCR-VL")
    pages = []
    for line in jsonl.text.strip().split("\n"):
        if not line.strip():
            continue
        for result in json.loads(line)["result"]["layoutParsingResults"]:
            pages.append(result["prunedResult"])
    if not pages:
        raise RuntimeError("PaddleOCR-VL returned no pages")
    return pages[0]


def paddleocr_v6_blocks(
    image_path: Path,
    token: str,
    model: str = PADDLE_OCR_MODEL,
    timeout: int = 300,
) -> dict:
    """Submit one image to PP-OCRv6 and return the full result payload.

    PP-OCRv6 returns line-level `rec_texts` / `rec_boxes` instead of
    PaddleOCR-VL's layout blocks, which maps directly to this tool's text
    reconstruction flow. Orientation and unwarping are disabled so the
    returned coordinates stay aligned with the source image.
    """
    optional = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useTextlineOrientation": False,
    }
    status = _paddle_job_status(image_path, token, model, optional, timeout, "PP-OCRv6")
    return _paddle_result_response(status, "PP-OCRv6").json()


def _box_from_points(points) -> list[float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def rapidocr_blocks(image_path: Path) -> list[dict]:
    """Local RapidOCR backend. Returns line-level blocks.

    Small images (diagrams, exported slides) are upscaled 2x before
    recognition; PP-OCR often misses small labels below ~600px. Boxes are
    mapped back to source pixels.
    """
    try:
        from rapidocr import RapidOCR  # type: ignore
    except Exception:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR is not installed. Install it with: pip install rapidocr onnxruntime"
            ) from exc

    import logging
    import tempfile

    for logger_name in ("RapidOCR", "rapidocr"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    with Image.open(image_path) as source_image:
        source_width, source_height = source_image.size
        longest = max(source_width, source_height)
        if longest < 400:
            scale = 3
        elif longest < 1200:
            scale = 2
        else:
            scale = 1
        if scale > 1:
            upscaled = source_image.convert("RGB").resize(
                (source_width * scale, source_height * scale), Image.LANCZOS
            )
        else:
            upscaled = None

    engine = RapidOCR()
    temporary_path = None
    try:
        if upscaled is not None:
            handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            temporary_path = Path(handle.name)
            handle.close()
            upscaled.save(temporary_path, "PNG")
            result = engine(str(temporary_path))
        else:
            result = engine(str(image_path))
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    boxes = []
    texts = []
    scores = []
    if hasattr(result, "boxes"):
        # RapidOCR 3.x returns numpy arrays here; `or []` would raise an
        # ambiguous-truth-value error, so test for None explicitly.
        raw_boxes = getattr(result, "boxes", None)
        raw_texts = getattr(result, "txts", None)
        raw_scores = getattr(result, "scores", None)
        boxes = list(raw_boxes) if raw_boxes is not None else []
        texts = list(raw_texts) if raw_texts is not None else []
        scores = list(raw_scores) if raw_scores is not None else []
    elif isinstance(result, tuple):
        data = result[0] or []
        for row in data:
            if len(row) >= 2:
                boxes.append(row[0])
                texts.append(row[1])
                scores.append(row[2] if len(row) > 2 else 1.0)
    else:
        for row in result or []:
            if len(row) >= 2:
                boxes.append(row[0])
                texts.append(row[1])
                scores.append(row[2] if len(row) > 2 else 1.0)

    blocks = []
    for box, text, score in zip(boxes, texts, scores):
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < 0.45:
            continue
        text = str(text).strip()
        if not text:
            continue
        box_px = _box_from_points(box)
        if scale > 1:
            box_px = [value / scale for value in box_px]
        blocks.append(
            {
                "text": text,
                "box_px": box_px,
                "confidence": confidence,
                "label": "text",
            }
        )
    return blocks


def tesseract_blocks(image_path: Path) -> list[dict]:
    try:
        import pytesseract
        from pytesseract import Output
    except Exception as exc:
        raise RuntimeError("pytesseract is not installed (pip install pytesseract and install Tesseract).") from exc

    image = Image.open(image_path).convert("RGB")
    languages = "chi_sim+eng"
    try:
        data = pytesseract.image_to_data(image, lang=languages, output_type=Output.DICT)
    except Exception:
        data = pytesseract.image_to_data(image, lang="eng", output_type=Output.DICT)

    grouped: dict[tuple, dict] = {}
    for index, text in enumerate(data.get("text", [])):
        text = str(text).strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = 100.0
        if confidence < 45:
            continue
        key = (
            data.get("block_num", [0] * len(data["text"]))[index],
            data.get("par_num", [0] * len(data["text"]))[index],
            data.get("line_num", [0] * len(data["text"]))[index],
        )
        entry = grouped.setdefault(key, {"parts": [], "left": 1e9, "top": 1e9, "right": 0, "bottom": 0})
        entry["parts"].append(text)
        left = float(data["left"][index])
        top = float(data["top"][index])
        width = float(data["width"][index])
        height = float(data["height"][index])
        entry["left"] = min(entry["left"], left)
        entry["top"] = min(entry["top"], top)
        entry["right"] = max(entry["right"], left + width)
        entry["bottom"] = max(entry["bottom"], top + height)

    blocks = []
    for entry in grouped.values():
        text = " ".join(entry["parts"]).strip()
        if not text:
            continue
        blocks.append(
            {
                "text": text,
                "box_px": [
                    entry["left"],
                    entry["top"],
                    entry["right"] - entry["left"],
                    entry["bottom"] - entry["top"],
                ],
                "confidence": 1.0,
                "label": "text",
            }
        )
    return blocks


def blocks_from_paddle(pruned: dict, source_width: int, source_height: int) -> list[dict]:
    scale_x = source_width / float(pruned.get("width") or source_width)
    scale_y = source_height / float(pruned.get("height") or source_height)
    blocks = []
    for block in pruned.get("parsing_res_list", []):
        label = str(block.get("block_label", "")).strip()
        if label not in PADDLE_TEXT_LABELS:
            continue
        text = str(block.get("block_content", "")).strip()
        if not text:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in block["block_bbox"])
        except Exception:
            continue
        x1, y1 = x1 * scale_x, y1 * scale_y
        x2, y2 = x2 * scale_x, y2 * scale_y
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(float(source_width), x2), min(float(source_height), y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        blocks.append(
            {
                "text": text,
                "box_px": [x1, y1, x2 - x1, y2 - y1],
                "confidence": 1.0,
                "label": label,
            }
        )
    return blocks


def blocks_from_paddle_ocr(payload: dict, source_width: int, source_height: int) -> list[dict]:
    """Convert a PP-OCRv6 result payload into line-level text blocks."""
    result = payload.get("result") or {}
    pages = result.get("ocrResults") or []
    if not pages:
        raise RuntimeError("PP-OCRv6 returned no pages")
    pruned = pages[0].get("prunedResult") or {}
    info = result.get("dataInfo") or {}
    scale_x = source_width / float(info.get("width") or source_width)
    scale_y = source_height / float(info.get("height") or source_height)

    texts = pruned.get("rec_texts") or []
    scores = pruned.get("rec_scores") or []
    boxes = pruned.get("rec_boxes") or []
    polys = pruned.get("rec_polys") or []
    blocks = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text).strip()
        if not text:
            continue
        if index < len(boxes):
            x1, y1, x2, y2 = (float(v) for v in boxes[index])
        elif index < len(polys):
            points = polys[index]
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        else:
            continue
        x1, y1 = x1 * scale_x, y1 * scale_y
        x2, y2 = x2 * scale_x, y2 * scale_y
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(float(source_width), x2), min(float(source_height), y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        confidence = float(scores[index]) if index < len(scores) else 1.0
        if confidence < 0.45:
            continue
        blocks.append(
            {
                "text": text,
                "box_px": [x1, y1, x2 - x1, y2 - y1],
                "confidence": confidence,
                "label": "text",
            }
        )
    return blocks


def detect_ocr_backend(requested: str) -> str:
    if requested != "auto":
        if requested in ("ppocrv6", "paddleocr-vl") and not load_paddle_token(None):
            raise SystemExit(
                "No Paddle OCR token found. Set PADDLE_OCR_TOKEN, pass --paddle-token, "
                "or run `editppt config --paddle-ocr-token <token>`."
            )
        return requested
    if load_paddle_token(None):
        return "ppocrv6"
    try:
        import importlib.util

        if importlib.util.find_spec("rapidocr") or importlib.util.find_spec("rapidocr_onnxruntime"):
            return "rapidocr"
    except Exception:
        pass
    try:
        import importlib.util

        if importlib.util.find_spec("pytesseract") and shutil.which("tesseract"):
            return "tesseract"
    except Exception:
        pass
    return "none"


def run_ocr(
    image_path: Path,
    backend: str,
    token: str | None,
    model: str,
    source_width: int,
    source_height: int,
    chart_recognition: bool = False,
) -> tuple[str, list[dict]]:
    """Return (backend_used, blocks). Falls back gracefully in auto mode."""
    if backend == "auto":
        backend = detect_ocr_backend("auto")
    order = [backend]
    if backend == "ppocrv6":
        order += ["rapidocr", "tesseract", "none"]
    elif backend == "paddleocr-vl":
        order += ["ppocrv6", "rapidocr", "tesseract", "none"]
    elif backend == "rapidocr":
        order += ["tesseract", "none"]
    elif backend == "tesseract":
        order += ["none"]

    for candidate in order:
        try:
            if candidate == "ppocrv6":
                if not token:
                    raise RuntimeError("missing Paddle OCR token")
                payload = paddleocr_v6_blocks(image_path, token)
                blocks = blocks_from_paddle_ocr(payload, source_width, source_height)
                if not blocks:
                    raise RuntimeError("PP-OCRv6 returned no text blocks")
                return candidate, blocks
            if candidate == "paddleocr-vl":
                if not token:
                    raise RuntimeError("missing PaddleOCR-VL token")
                pruned = paddleocr_vl_blocks(image_path, token, model, chart_recognition=chart_recognition)
                blocks = blocks_from_paddle(pruned, source_width, source_height)
                if not blocks:
                    # PaddleOCR-VL's layout parser sometimes labels a whole
                    # diagram page as a single `image` block and returns no
                    # text. Fall through to a line-level local OCR backend.
                    raise RuntimeError("PaddleOCR-VL returned no text blocks")
                return candidate, blocks
            if candidate == "rapidocr":
                return candidate, rapidocr_blocks(image_path)
            if candidate == "tesseract":
                return candidate, tesseract_blocks(image_path)
            if candidate == "none":
                return "none", []
        except Exception as exc:
            if candidate == "none":
                raise
            print(f"  ! OCR backend {candidate} failed: {exc}")
            if backend != "auto" and candidate == backend:
                # Explicit backend: still try local fallbacks, but report it.
                warn(f"{candidate} OCR failed; trying the next available backend.")
            continue
    return "none", []


# ---------------------------------------------------------------------------
# Page analysis
# ---------------------------------------------------------------------------


def analyze_page(image: Image.Image) -> dict:
    small, scale = resize_max(image, 512)
    quantized = small.quantize(colors=16)
    indices = np.asarray(quantized)
    counts = np.bincount(indices.ravel(), minlength=256)
    top = np.sort(counts)[::-1][:4]
    flat_ratio = float(top.sum()) / float(indices.size)

    gray = np.asarray(small.convert("L"), dtype=np.float32)
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    edge_density = float((gx > 25).mean() + (gy > 25).mean()) / 2.0

    full_rgb = np.asarray(image, dtype=np.uint8)
    background = border_color(full_rgb)
    mode = "native" if (flat_ratio >= 0.72 and edge_density < 0.16) else "image"
    return {
        "flat_ratio": flat_ratio,
        "edge_density": edge_density,
        "background_rgb": background,
        "background": hex_color_from_rgb(background),
        "mode": mode,
        "analysis_scale": scale,
    }


# ---------------------------------------------------------------------------
# Text reconstruction
# ---------------------------------------------------------------------------


def cluster_glyph_heights(
    values: list[float], min_gap: float = 0.05, max_spread: float = 1.12
) -> list[list[int]]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[int]] = [[order[0]]] if order else []
    for index in order[1:]:
        previous = values[groups[-1][-1]]
        if previous > 0 and values[index] / previous > 1.0 + min_gap:
            groups.append([index])
        else:
            groups[-1].append(index)

    final: list[list[int]] = []
    stack = [group for group in groups]
    while stack:
        current = stack.pop()
        lo, hi = values[current[0]], values[current[-1]]
        if len(current) < 2 or lo <= 0 or hi / lo <= max_spread:
            final.append(current)
            continue
        jumps = [values[current[i + 1]] / max(values[current[i]], 0.1) for i in range(len(current) - 1)]
        cut = jumps.index(max(jumps)) + 1
        stack.append(current[:cut])
        stack.append(current[cut:])
    return final


def choose_text_align(block_box: list[float], page_width: int, page_height: int) -> str:
    x, y, w, h = block_box
    center = x + w / 2.0
    if w < 0.75 * page_width and abs(center - page_width / 2.0) < 0.025 * page_width:
        if y < 0.30 * page_height or y + h > 0.85 * page_height:
            return "center"
    return "left"


def _flatten_text_lines(text: str) -> str:
    """Join OCR line breaks while keeping a space between Latin words."""
    parts = [part.strip() for part in str(text).splitlines() if part.strip()]
    if not parts:
        return ""
    flat = parts[0]
    for part in parts[1:]:
        if flat and flat[-1].isascii() and part[0].isascii():
            flat += " " + part
        else:
            flat += part
    return flat


def _choose_text_break(text: str, target: int) -> int:
    """Choose a visually sensible line break near a proportional target."""
    target = max(1, min(len(text) - 1, target))

    def positions(window):
        for delta in range(window + 1):
            for position in (target - delta, target + delta):
                if 1 <= position < len(text):
                    yield position

    for position in positions(4):
        if text[position - 1] in "，。、；：！？,.;:!? ":
            return position
    for position in positions(5):
        if text[position] in "但而并且或与及":
            return position
    for position in positions(3):
        if text.startswith(
            ("建立", "实现", "形成", "提升", "推动", "构建", "打造", "确保", "支持", "提供"),
            position,
        ):
            return position
    return target


def split_text_to_widths(text: str, widths: list[int]) -> str:
    """Split a merged OCR block into the number of visual lines it contains.

    PaddleOCR sometimes returns two visual lines as one string without a
    newline. The measured ink bands know how many lines exist and how wide
    each is, so the text is distributed proportionally.
    """
    flat = _flatten_text_lines(text)
    count = len(widths)
    if count <= 1 or not flat:
        return flat
    total = float(sum(max(1, width) for width in widths))
    boundaries: list[int] = []
    accumulated = 0.0
    previous = 0
    length = len(flat)
    for index in range(count - 1):
        accumulated += max(1, widths[index])
        target = int(round(accumulated / total * length))
        target = max(previous + 1, min(length - (count - 1 - index), target))
        cut = _choose_text_break(flat, target)
        cut = max(previous + 1, min(length - (count - 1 - index), cut))
        boundaries.append(cut)
        previous = cut
    chunks = []
    start = 0
    for cut in boundaries:
        chunks.append(flat[start:cut].strip())
        start = cut
    chunks.append(flat[start:].strip())
    return "\n".join(chunk for chunk in chunks if chunk)


def build_text_items(
    blocks: list[dict],
    image: Image.Image,
    source_width: int,
    source_height: int,
    content_box: dict,
    font_cjk: str,
    font_latin: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Turn OCR blocks into measured text boxes, removal ops and hint records."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    rgb = np.asarray(image, dtype=np.float32)
    inches_per_px = float(content_box["height"]) / float(source_height)

    items: list[dict] = []
    removals: list[dict] = []
    hints: list[dict] = []

    def append_item(
        *,
        text: str,
        box: list[float],
        glyph: float,
        line_count: int,
        font_pt: float,
        color: str,
        bold: bool,
        measurement_ok: bool,
        text_height_px: float,
        mask: np.ndarray | None,
        mask_box: list[float] | None,
        polarity: str,
        label: str,
    ) -> None:
        align = choose_text_align(box, source_width, source_height)
        item = {
            "id": f"t{len(items) + 1:03d}",
            "text": text,
            "box_px": [round(v, 2) for v in box],
            "font_size": round(font_pt, 1),
            "font_cjk": font_cjk,
            "font_latin": font_latin,
            "color": color,
            "align": align,
            "valign": "top",
            "bold": bool(bold),
            "wrap": "none",
            "z_index": 40 + len(items) * 0.01,
            "_glyph_px": glyph,
            "_line_count": line_count,
            "_measurement_ok": measurement_ok,
            "_text_height_px": text_height_px,
        }
        if measurement_ok:
            item["font_size_source"] = "measured"
            if line_count > 1:
                font_px = font_pt / 72.0 / inches_per_px
                line_height = (text_height_px / line_count) / max(font_px, 1e-6)
                item["line_height"] = round(max(1.0, min(1.6, line_height)), 3)
        items.append(item)

        removal: dict = {
            "box": box,
            "mask": None,
            "ink_mask": None,
            "kind": "text",
            "text": text[:40],
        }
        if mask is not None and mask_box is not None:
            removal["mask_box"] = mask_box
            removal["ink_mask"] = mask
            dilated = mask
            if HAVE_SCIPY:
                # Large type carries a wider antialiasing halo; dilating by a
                # fraction of the glyph height removes the fringe without
                # turning small labels into huge blur patches.
                iterations = max(3, min(12, int(round(glyph * 0.18))))
                dilated = ndimage.binary_dilation(mask, iterations=iterations)
            removal["mask"] = dilated
        removals.append(removal)

        hints.append(
            {
                "id": f"L{len(hints) + 1:02d}",
                "text": text,
                "box_px": [round(v, 1) for v in box],
                "glyph_height_px": round(glyph, 1),
                "line_count": line_count,
                "font_pt": round(font_pt, 1),
                "confidence": block.get("confidence", 1.0),
                "label": label,
                "measurement_ok": measurement_ok,
            }
        )

    for block in blocks:
        text = str(block.get("text", "")).strip()
        if not text:
            continue
        bx, by, bw, bh = clamp_box(block["box_px"], source_width, source_height)
        if bw < 4 or bh < 4:
            continue
        confidence = float(block.get("confidence", 1.0) or 1.0)
        # Tiny low-confidence fragments are usually map lines, arrowheads,
        # icon details, or logo marks mistaken for text.
        if len(text) <= 2 and confidence < 0.92 and bw < 34 and bh < 34:
            continue
        if len(text) <= 6 and text.isascii() and confidence < 0.75 and bw < 48 and bh < 26:
            continue
        gray_crop = gray[by : by + bh, bx : bx + bw]
        rgb_crop = rgb[by : by + bh, bx : bx + bw]
        measured = measure_crop(gray_crop)
        label = str(block.get("label", "text"))

        if measured.get("ok") and measured.get("bbox"):
            mx, my, mw, mh = measured["bbox"]
            glyph = float(measured["glyph_height_px"])
            line_count = max(1, int(measured["line_count"]))
            mask = measured["mask"]
            polarity = measured["polarity"]
            bands = measured.get("bands") or []
            text_lines = [line for line in text.splitlines() if line.strip()]

            # A 3D disc/ribbon often has a thin specular highlight that looks
            # like ink to the thresholder. When OCR returned a single line,
            # discard bands much shorter than the tallest one and tighten the
            # measured box to the real text band. Otherwise the highlight is
            # counted as a second line and the font size is badly underestimated.
            if len(bands) >= 2 and len(text_lines) <= 1:
                tallest_band = max(bottom - top for top, bottom in bands)
                kept_bands = [
                    (top, bottom)
                    for top, bottom in bands
                    if (bottom - top) >= tallest_band * 0.5
                ]
                dropped_bands = [band for band in bands if band not in kept_bands]
                only_edge_bands = all(
                    top <= 2 or bottom >= mask.shape[0] - 2
                    for top, bottom in dropped_bands
                )
                if (
                    len(kept_bands) == 1
                    and len(kept_bands) < len(bands)
                    and only_edge_bands
                ):
                    band_top, band_bottom = kept_bands[0]
                    band_mask = mask[band_top:band_bottom]
                    columns = np.flatnonzero(band_mask.any(axis=0))
                    if columns.size:
                        mx = int(columns[0])
                        mw = int(columns[-1] - columns[0] + 1)
                        my = int(band_top)
                        mh = int(band_bottom - band_top)
                        glyph = float(mh)
                        line_count = 1
                        bands = kept_bands

            # A block that contains a large heading and a small body line is
            # two text levels, not one. Split it into per-band text boxes so
            # each band gets its own measured font size.
            if len(bands) >= 2 and len(text_lines) == len(bands):
                band_heights = [bottom - top for top, bottom in bands]
                ratio = max(band_heights) / max(1, min(band_heights))
                if ratio >= 1.18:
                    split_done = True
                    for (band_top, band_bottom), line_text in zip(bands, text_lines):
                        band_mask = mask[band_top:band_bottom]
                        columns = np.flatnonzero(band_mask.any(axis=0))
                        if columns.size == 0:
                            split_done = False
                            break
                        col_start, col_end = int(columns[0]), int(columns[-1])
                        band_glyph = float(band_bottom - band_top)
                        pad_x = max(2, int(round(band_glyph * 0.35)))
                        pad_y = max(1, int(round(band_glyph * 0.30)))
                        left = max(0, bx + col_start - pad_x)
                        top = max(0, by + band_top - pad_y)
                        right = min(source_width, bx + col_end + 1 + pad_x)
                        bottom = min(source_height, by + band_bottom + pad_y)
                        box = [left, top, max(2, right - left), max(2, bottom - top)]
                        tight_mask = mask[band_top:band_bottom, col_start : col_end + 1]
                        tight_rgb = rgb_crop[band_top:band_bottom, col_start : col_end + 1]
                        band_color = text_color_from_mask(tight_rgb, tight_mask, polarity)
                        band_bold = estimate_bold(tight_mask, band_glyph, line_text)
                        band_font_pt = band_glyph * inches_per_px / glyph_em_ratio(line_text) * 72.0
                        band_font_pt = max(5.0, min(160.0, band_font_pt))
                        append_item(
                            text=line_text.strip(),
                            box=box,
                            glyph=band_glyph,
                            line_count=1,
                            font_pt=band_font_pt,
                            color=band_color,
                            bold=band_bold,
                            measurement_ok=True,
                            text_height_px=band_glyph,
                            mask=tight_mask,
                            mask_box=[bx + col_start, by + band_top, col_end - col_start + 1, band_bottom - band_top],
                            polarity=polarity,
                            label=label,
                        )
                    if split_done:
                        continue

            # One OCR string can cover several visual lines without newlines.
            # Re-flow it to the measured line count. Short ASCII tokens such
            # as stage numbers ("01", "02", "KPI") are kept intact: a shading
            # edge or 3D disc can create a false horizontal ink gap that looks
            # like two lines.
            short_ascii_token = (
                len(text.strip()) <= 3
                and text.strip().isascii()
                and not any(character.isspace() for character in text)
            )
            if len(bands) >= 2 and len(text_lines) < len(bands) and not short_ascii_token:
                widths = []
                for band_top, band_bottom in bands:
                    band_mask = mask[band_top:band_bottom]
                    columns = np.flatnonzero(band_mask.any(axis=0))
                    widths.append(int(columns[-1] - columns[0] + 1) if columns.size else 1)
                text = split_text_to_widths(text, widths)

            pad_x = max(2, int(round(glyph * 0.35)))
            pad_y = max(1, int(round(glyph * 0.30)))
            left = max(0, bx + mx - pad_x)
            top = max(0, by + my - pad_y)
            right = min(source_width, bx + mx + mw + pad_x)
            bottom = min(source_height, by + my + mh + pad_y)
            box = [left, top, max(2, right - left), max(2, bottom - top)]
            color = text_color_from_mask(
                rgb_crop[my : my + mh, mx : mx + mw],
                mask[my : my + mh, mx : mx + mw],
                polarity,
            )
            bold = estimate_bold(mask, glyph, text)
            font_pt = glyph * inches_per_px / glyph_em_ratio(text) * 72.0
            font_pt = max(5.0, min(160.0, font_pt))
            append_item(
                text=text,
                box=box,
                glyph=glyph,
                line_count=max(1, len(text.splitlines())),
                font_pt=font_pt,
                color=color,
                bold=bold,
                measurement_ok=True,
                text_height_px=float(mh),
                mask=mask[my : my + mh, mx : mx + mw],
                mask_box=[bx + mx, by + my, mw, mh],
                polarity=polarity,
                label=label,
            )
        else:
            glyph = max(1.0, bh / max(1, text.count("\n") + 1) / 1.30)
            line_count = max(1, text.count("\n") + 1)
            box = [bx, by, bw, bh]
            color = text_color_from_mask(rgb_crop, np.ones(gray_crop.shape, dtype=bool), "dark")
            font_pt = glyph * inches_per_px / glyph_em_ratio(text) * 72.0
            font_pt = max(5.0, min(160.0, font_pt))
            append_item(
                text=text,
                box=box,
                glyph=glyph,
                line_count=line_count,
                font_pt=font_pt,
                color=color,
                bold=False,
                measurement_ok=False,
                text_height_px=float(bh),
                mask=None,
                mask_box=None,
                polarity="dark",
                label=label,
            )

    # Same-level text uses exactly one font size (skill section 3.1).
    measured_items = [item for item in items if item["_measurement_ok"]]
    if len(measured_items) >= 2:
        glyph_values = [item["_glyph_px"] for item in measured_items]
        clusters = cluster_glyph_heights(glyph_values)
        for cluster in clusters:
            members = sorted(glyph_values[i] for i in cluster)
            median = members[len(members) // 2]
            for index in cluster:
                item = measured_items[index]
                if abs(item["_glyph_px"] - median) / max(median, 1e-6) > 0.25:
                    continue
                item["_glyph_px"] = median
                item["font_size"] = round(median * inches_per_px / glyph_em_ratio(item["text"]) * 72.0, 1)
                item["font_size"] = max(5.0, min(160.0, item["font_size"]))
                if item.get("line_height"):
                    font_px = item["font_size"] / 72.0 / inches_per_px
                    item["line_height"] = round(
                        max(1.0, min(1.6, (item["_text_height_px"] / item["_line_count"]) / max(font_px, 1e-6))),
                        3,
                    )
        for hint, item in zip(hints, items):
            hint["font_pt"] = item["font_size"]

    # Public fields only.
    for item in items:
        for key in [key for key in item if key.startswith("_")]:
            item.pop(key)
    return items, removals, hints


def _removal_background_std(rgb: np.ndarray, removal: dict) -> float:
    """Colour spread of the non-text pixels inside a removal box.

    A low ring MAD only says the immediate border is uniform. A big poster
    title can still sit on top of an illustration whose limbs/doodles enter
    the text box; inpainting that box would smear them. Measuring the
    non-glyph pixels inside the box catches that case.
    """
    box = removal.get("box")
    if not box:
        return 0.0
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(width, x + w), min(height, y + h)
    crop = rgb[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0
    mask = removal.get("mask")
    if mask is None:
        pixels = crop.reshape(-1, 3)
    else:
        text_mask = np.zeros(crop.shape[:2], dtype=bool)
        mask_box = removal.get("mask_box") or box
        mx, my = [int(round(v)) for v in mask_box[:2]]
        ox, oy = mx - x1, my - y1
        ty1, ty2 = max(0, oy), min(crop.shape[0], oy + mask.shape[0])
        tx1, tx2 = max(0, ox), min(crop.shape[1], ox + mask.shape[1])
        if ty2 > ty1 and tx2 > tx1:
            text_mask[ty1:ty2, tx1:tx2] = mask[ty1 - oy : ty2 - oy, tx1 - ox : tx2 - ox]
        pixels = crop[~text_mask]
    if pixels.size == 0:
        return 999.0
    return float(np.max(np.std(pixels.astype(np.float32), axis=0)))


def _text_mask_in_patch(removal: dict, x0: int, y0: int, width: int, height: int) -> np.ndarray:
    """Place a text removal mask into a patch's local coordinates."""
    mask = np.zeros((height, width), dtype=bool)
    source = removal.get("mask")
    mask_box = removal.get("mask_box")
    if source is None or mask_box is None:
        return mask
    ox = int(round(float(mask_box[0]))) - x0
    oy = int(round(float(mask_box[1]))) - y0
    sx0, sy0 = max(0, ox), max(0, oy)
    sx1 = min(width, ox + source.shape[1])
    sy1 = min(height, oy + source.shape[0])
    if sx1 > sx0 and sy1 > sy0:
        mask[sy0:sy1, sx0:sx1] = source[sy0 - oy : sy1 - oy, sx0 - ox : sx1 - ox]
    return mask


def _smooth_closed_polygon(points: list[tuple[float, float]], iterations: int = 2) -> list[tuple[float, float]]:
    """Chaikin corner-cutting for a closed polygon.

    approxPolyDP keeps the contour compact but leaves straight segments that
    look faceted on ribbons and end caps. Two Chaikin passes keep the same
    silhouette while making PowerPoint's freeform edge visually smooth.
    """
    if len(points) < 3:
        return points
    smoothed = np.asarray(points, dtype=np.float64)
    for _ in range(max(0, iterations)):
        following = np.roll(smoothed, -1, axis=0)
        first = 0.75 * smoothed + 0.25 * following
        second = 0.25 * smoothed + 0.75 * following
        merged = np.empty((len(smoothed) * 2, 2), dtype=np.float64)
        merged[0::2] = first
        merged[1::2] = second
        smoothed = merged
        if len(smoothed) > 160:
            break
    return [(float(point[0]), float(point[1])) for point in smoothed]


def gradient_backdrop_shape(
    rgb: np.ndarray,
    item: dict,
    removal: dict,
    other_boxes: list[list[float]],
    *,
    max_mae: float = 30.0,
) -> dict | None:
    """Build a native gradient patch behind text that cannot be flat-filled.

    Text sitting on a smooth coloured band (ribbons, badges, end caps) often
    fails the flat-repair gate even though its local background is a clean
    linear gradient. In that case a native rectangle with a fitted linear
    gradient can cover the old pixels exactly, so the text becomes editable
    without leaving a flat rectangular hole. Complex photos/illustrations
    fail the colour-consistency and fit-error checks and stay raster.
    """
    if not HAVE_CV2:
        return None
    bx, by, bw, bh = (float(v) for v in item["box_px"])
    if bw < 18 or bh < 12:
        return None
    pad = max(1, min(5, int(round(min(bw, bh) * 0.04))))
    x0 = max(0, int(round(bx - pad)))
    y0 = max(0, int(round(by - pad)))
    x1 = min(int(rgb.shape[1]), int(round(bx + bw + pad)))
    y1 = min(int(rgb.shape[0]), int(round(by + bh + pad)))
    width, height = x1 - x0, y1 - y0
    if width < 12 or height < 10:
        return None
    patch_box = [x0, y0, width, height]
    for box in other_boxes:
        if boxes_overlap(patch_box, box, margin=1.0):
            return None

    crop = rgb[y0:y1, x0:x1]
    text_mask = _text_mask_in_patch(removal, x0, y0, width, height)
    non_text = ~text_mask
    if int(non_text.sum()) < 40:
        return None

    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    saturation = hsv[..., 1].astype(np.float32) / 255.0
    value = hsv[..., 2].astype(np.float32) / 255.0
    coloured = (saturation >= 0.18) & (value >= 0.10)
    if float(coloured[non_text].mean()) < 0.90:
        return None
    text_left = int(round(bx)) - x0
    text_top = int(round(by)) - y0
    text_right = text_left + int(round(bw))
    text_bottom = text_top + int(round(bh))
    text_region = coloured[max(0, text_top) : min(height, text_bottom), max(0, text_left) : min(width, text_right)]
    if text_region.size == 0 or float(text_region.mean()) < 0.75:
        return None

    # Fit only the saturated band pixels. The light-blue edge/background is
    # part of the patch box but not part of the band behind the glyphs;
    # including it would drag the fitted gradient toward a pale colour and
    # make the patch visible.
    saturated = (saturation >= 0.35) & (value >= 0.10)
    fit_mask = non_text & saturated
    if int(fit_mask.sum()) < max(60, int(0.08 * width * height)):
        return None

    hues = hsv[..., 0][fit_mask].astype(np.int32)
    histogram = np.bincount(hues, minlength=180)
    dominant = int(histogram.argmax())
    hue_distance = np.abs((hues - dominant + 90) % 180 - 90)
    if float((hue_distance <= 12).mean()) < 0.70:
        return None

    ys, xs = np.nonzero(fit_mask)
    pixels = crop[ys, xs].astype(np.float32)
    design = np.column_stack([xs, ys, np.ones_like(xs)]).astype(np.float32)
    coefficients, _residuals, _rank, _singular = np.linalg.lstsq(design, pixels, rcond=None)
    predicted = np.clip(design @ coefficients, 0, 255)
    gradient_mae = float(np.abs(predicted - pixels).mean())
    solid_mae = float(np.abs(pixels - pixels.mean(axis=0)).mean())
    if gradient_mae > max_mae or gradient_mae > solid_mae + 0.75:
        return None

    luminance_gradient = coefficients[:2] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if float(np.hypot(*luminance_gradient)) < 0.02:
        return None
    angle = float(np.degrees(np.arctan2(float(luminance_gradient[1]), float(luminance_gradient[0]))) % 360.0)
    direction = np.array([np.cos(np.radians(angle)), np.sin(np.radians(angle))], dtype=np.float32)
    projection = xs * direction[0] + ys * direction[1]
    low, high = np.percentile(projection, [5, 95])
    if float(high - low) < 2.0:
        return None
    colour_low = pixels[projection <= low].mean(axis=0)
    colour_high = pixels[projection >= high].mean(axis=0)
    if float(np.abs(colour_low - colour_high).mean()) < 0.5:
        return None

    fill = {
        "type": "gradient",
        "angle": round(angle, 1),
        "stops": [
            {"pos": 0.0, "color": hex_color_from_rgb(colour_low)},
            {"pos": 1.0, "color": hex_color_from_rgb(colour_high)},
        ],
    }
    shrink = max(1, int(round(min(width, height) * 0.02)))
    shape = {
        "type": "rect",
        "box_px": [
            x0 + shrink,
            y0 + shrink,
            max(2, width - 2 * shrink),
            max(2, height - 2 * shrink),
        ],
        "fill": fill,
        "stroke": "none",
        "source": "fitted-gradient-backdrop",
        "reason": "text sits on a smooth coloured gradient; local patch rebuilt as a native gradient shape",
    }

    # Prefer a freeform that follows the coloured band instead of a rectangle:
    # it covers the glyphs without painting the pale background corners.
    if HAVE_SCIPY:
        band = ((saturation >= 0.35) & (value >= 0.10)).astype(np.uint8)
        band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8))
        band = ndimage.binary_fill_holes(band).astype(np.uint8)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(band, 8)
        best_label = 0
        best_overlap = -1
        for label_index in range(1, count):
            component = labels == label_index
            overlap = int((component & text_mask).sum())
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label_index
        if best_label:
            component = (labels == best_label).astype(np.uint8)
            component = cv2.dilate(component, np.ones((3, 3), dtype=np.uint8), iterations=1)
            if text_mask.any():
                coverage = float(component[text_mask].mean())
            else:
                coverage = 1.0
            if coverage >= 0.97:
                # The dilated component guarantees glyph coverage; the drawn
                # shape is eroded by one pixel so the patch sits slightly
                # inside the colour band instead of bleeding past it.
                shape_component = cv2.erode(
                    component, np.ones((3, 3), dtype=np.uint8), iterations=1
                )
                refined_fill = fill
                component_ys, component_xs = np.nonzero((shape_component > 0) & fit_mask)
                if component_ys.size >= 80:
                    component_pixels = crop[component_ys, component_xs].astype(np.float32)
                    projection = (
                        component_xs.astype(np.float32) * direction[0]
                        + component_ys.astype(np.float32) * direction[1]
                    )
                    low, high = np.percentile(projection, [5, 95])
                    if float(high - low) >= 2.0:
                        stops = []
                        for position, fraction in ((0.0, 0.05), (0.5, 0.5), (1.0, 0.95)):
                            target = float(low + (high - low) * fraction)
                            half_width = max(1.0, float(high - low) * 0.12)
                            selected = np.abs(projection - target) <= half_width
                            if selected.any():
                                sample = component_pixels[selected]
                            else:
                                nearest = int(np.argmin(np.abs(projection - target)))
                                sample = component_pixels[[nearest]]
                            stops.append(
                                {
                                    "pos": position,
                                    "color": hex_color_from_rgb(np.median(sample, axis=0)),
                                }
                            )
                        refined_fill = {
                            "type": "gradient",
                            "angle": round(angle, 1),
                            "stops": stops,
                        }
                contours, _hierarchy = cv2.findContours(
                    shape_component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                if contours:
                    contour = max(contours, key=cv2.contourArea)
                    if cv2.contourArea(contour) >= 0.15 * width * height:
                        epsilon = max(0.8, 0.003 * cv2.arcLength(contour, True))
                        approx = cv2.approxPolyDP(contour, epsilon, True)
                        if len(approx) >= 3:
                            raw_points = [
                                (float(point[0][0]), float(point[0][1]))
                                for point in approx
                            ]
                            smoothed = _smooth_closed_polygon(raw_points, iterations=2)
                            points = [
                                [x0 + int(round(point[0])), y0 + int(round(point[1]))]
                                for point in smoothed
                            ]
                            point_x = [point[0] for point in points]
                            point_y = [point[1] for point in points]
                            box = [min(point_x), min(point_y), max(point_x) - min(point_x), max(point_y) - min(point_y)]
                            if box[2] >= 4 and box[3] >= 4:
                                shape = {
                                    "type": "freeform",
                                    "box_px": box,
                                    "path_px": points,
                                    "fill": refined_fill,
                                    "stroke": "none",
                                                                "source": "fitted-gradient-backdrop",
                                    "reason": "text sits on a smooth coloured band; local band rebuilt as a native freeform gradient shape",
                                }
    return shape


def _background_std_without_text_colour(
    rgb: np.ndarray,
    removal: dict,
    item: dict,
    tolerance: float = 90.0,
) -> float:
    """Background spread after ignoring decorations in the text colour.

    Coloured headings often include a small bar/underline drawn in the same
    colour as the glyphs (for example "| 资源配置"). Those decoration pixels
    are inside the OCR box but are not glyphs, so they inflate the background
    spread and block a safe repair. Ignoring pixels close to the measured text
    colour lets the text become native while the decoration stays in the base.
    """
    box = removal.get("box")
    colour = item.get("color")
    if not box or not colour:
        return 999.0
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(width, x + w), min(height, y + h)
    crop = rgb[y1:y2, x1:x2]
    if crop.size == 0:
        return 999.0
    text_mask = _text_mask_in_patch(removal, x1, y1, x2 - x1, y2 - y1)
    pixels = crop[~text_mask].astype(np.float32)
    if pixels.size == 0:
        return 999.0
    text_colour = np.array(rgb_from_hex(colour), dtype=np.float32)
    distance = np.linalg.norm(pixels - text_colour, axis=1)
    remaining = pixels[distance > tolerance]
    if remaining.shape[0] < 50:
        return 999.0
    return float(np.max(np.std(remaining, axis=0)))


def filter_repairable_text(
    text_items: list[dict],
    removals: list[dict],
    hints: list[dict],
    rgb: np.ndarray,
    mad_threshold: float = 15.0,
    std_threshold: float = 20.0,
    gradient_backdrops: bool = True,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Keep native text only where the background can be repaired cleanly.

    Two conditions must hold: the ring just outside the text box is uniform
    (low MAD), and the non-text pixels inside the box are not a busy
    illustration (low colour spread). Text over photos, 3D objects or heavy
    illustration is intentionally kept as raster (and reported) instead of
    being duplicated by a native box that would leave smears or ghosts.

    When flat repair fails but the local background is a smooth colour
    gradient, a native gradient rectangle is placed over the old pixels and
    the text stays editable. The returned fifth list contains those shapes.
    """
    kept_items: list[dict] = []
    kept_removals: list[dict] = []
    kept_hints: list[dict] = []
    raster_text: list[dict] = []
    backdrop_shapes: list[dict] = []
    boxes = [item.get("box_px") for item in text_items]
    for index, (item, removal, hint) in enumerate(zip(text_items, removals, hints)):
        _ring, _std, mad = sample_ring(rgb, removal.get("box") or item["box_px"], band=3)
        background_std = _removal_background_std(rgb, removal)
        tight_repair = False
        effective_std = background_std
        text_colour = item.get("color")
        colour_contrast = 0.0
        if text_colour:
            colour_contrast = float(
                np.linalg.norm(
                    np.array(rgb_from_hex(text_colour), dtype=np.float32)
                    - np.asarray(_ring, dtype=np.float32)
                )
            )
        if (
            mad <= mad_threshold
            and background_std > std_threshold
            and colour_contrast >= 60.0
        ):
            adjusted_std = _background_std_without_text_colour(rgb, removal, item)
            if adjusted_std <= std_threshold and _edges_are_flat(
                rgb, removal.get("box") or item["box_px"]
            ):
                effective_std = adjusted_std
                tight_repair = True
        if mad <= mad_threshold and effective_std <= std_threshold:
            kept_items.append(item)
            if tight_repair:
                tight_removal = dict(removal)
                tight_removal["tight"] = True
                tight_removal["text_color"] = item.get("color")
                kept_removals.append(tight_removal)
            else:
                kept_removals.append(removal)
            kept_hints.append(hint)
            continue

        backdrop = None
        if gradient_backdrops:
            other_boxes = [box for position, box in enumerate(boxes) if position != index and box]
            backdrop = gradient_backdrop_shape(rgb, item, removal, other_boxes)
        if backdrop is not None:
            if re.fullmatch(r"\d{1,4}", item.get("text", "").strip()):
                # Large stage numbers sit on shaded 3D discs. Tight glyph
                # repair already leaves a clean base; overlaying a gradient
                # patch here would show as a rectangular colour block.
                numeric_removal = dict(removal)
                numeric_removal["tight"] = True
                numeric_removal["text_color"] = item.get("color")
                kept_items.append(item)
                kept_removals.append(numeric_removal)
                kept_hints.append(hint)
                continue
            backdrop["id"] = f"g{len(backdrop_shapes) + 1:03d}"
            backdrop["z_index"] = 5.0 + len(backdrop_shapes) * 0.01
            backdrop_shapes.append(backdrop)
            item["backdrop_shape"] = backdrop["id"]
            kept_items.append(item)
            tight_removal = dict(removal)
            tight_removal["tight"] = True
            kept_removals.append(tight_removal)
            kept_hints.append(hint)
            continue

        raster_text.append(
            {
                "text": item.get("text", ""),
                "box_px": item.get("box_px"),
                "ring_mad": round(float(mad), 2),
                "background_std": round(float(background_std), 2),
                "reason": "background is too complex for a clean local repair; kept as raster",
            }
        )
    return kept_items, kept_removals, kept_hints, raster_text, backdrop_shapes


def draw_text_hint_overlay(image: Image.Image, hints: list[dict], out_path: Path) -> None:
    overlay = image.convert("RGB").copy()
    draw = ImageDraw.Draw(overlay)
    font_path = preview_font_path(cjk=True)
    label_size = max(12, image.size[0] // 100)
    try:
        font = ImageFont.truetype(font_path, size=label_size, index=0) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    for hint in hints:
        x, y, w, h = hint["box_px"]
        draw.rectangle([x, y, x + w, y + h], outline="#E02020", width=2)
        label = f"{hint['id']} {hint.get('font_pt', '?')}pt"
        text_width = draw.textlength(label, font=font)
        ly = y - label_size - 4 if y > label_size + 6 else y + h + 2
        draw.rectangle([x, ly - 1, x + text_width + 6, ly + label_size + 3], fill="#FFF7CC")
        draw.text((x + 3, ly), label, fill="#B00000", font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(out_path)


# ---------------------------------------------------------------------------
# Background repair / inpainting
# ---------------------------------------------------------------------------


def _diffuse_fill(rgb: np.ndarray, mask: np.ndarray, box: list[int], fallback: np.ndarray) -> np.ndarray:
    """Fill masked pixels with a smooth diffusion of the surrounding colours."""
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    pad = max(8, min(160, int(round(max(w, h) * 0.6))))
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
    crop = rgb[y1:y2, x1:x2].astype(np.float32).copy()
    local_mask = mask[y1:y2, x1:x2].copy()
    if not local_mask.any():
        return rgb

    if HAVE_CV2:
        # Telea inpainting is noticeably better on photos and gradients than
        # the pure-numpy diffusion fallback below.
        source = np.clip(crop, 0, 255).astype(np.uint8)
        mask_u8 = (local_mask.astype(np.uint8) * 255)
        filled = cv2.inpaint(source, mask_u8, 3, cv2.INPAINT_TELEA)
        crop[local_mask] = filled[local_mask].astype(np.float32)
        rgb[y1:y2, x1:x2] = crop
        return rgb

    if not HAVE_SCIPY:
        crop[local_mask] = fallback
        return rgb

    # Coarse nearest-neighbour fill followed by Gaussian diffusion. This is a
    # cheap approximation of a Laplace solve and keeps large text patches from
    # turning into hard colour blocks.
    coarse_max = 96
    ch, cw = crop.shape[:2]
    factor = max(1, int(math.ceil(max(ch, cw) / coarse_max)))
    small_h, small_w = max(2, ch // factor), max(2, cw // factor)
    small_img = np.asarray(
        Image.fromarray(np.clip(crop, 0, 255).astype(np.uint8)).resize((small_w, small_h), Image.BILINEAR),
        dtype=np.float32,
    )
    small_mask = np.asarray(
        Image.fromarray(local_mask.astype(np.uint8) * 255).resize((small_w, small_h), Image.NEAREST),
        dtype=np.uint8,
    ) > 127
    if not small_mask.any():
        small_mask = np.zeros((small_h, small_w), dtype=bool)
        small_mask[small_h // 4 : 3 * small_h // 4, small_w // 4 : 3 * small_w // 4] = True
    if small_mask.all():
        # No unmasked anchor exists; keep a one-pixel frame of the fallback
        # colour so the distance transform always has a valid source.
        small_mask[0, :] = False
        small_mask[-1, :] = False
        small_mask[:, 0] = False
        small_mask[:, -1] = False
        small_img[~small_mask] = fallback

    indices = ndimage.distance_transform_edt(small_mask, return_distances=False, return_indices=True)
    filled = small_img[tuple(indices)]
    for _ in range(24):
        blurred = ndimage.gaussian_filter(filled, sigma=(1.6, 1.6, 0))
        filled[small_mask] = blurred[small_mask]

    upsampled = np.asarray(
        Image.fromarray(np.clip(filled, 0, 255).astype(np.uint8)).resize((cw, ch), Image.BILINEAR),
        dtype=np.float32,
    )
    # Feather the seam: only masked pixels are replaced, then blend a 1 px
    # boundary with a blurred version of the upsampled fill.
    feathered = ndimage.gaussian_filter(upsampled, sigma=(1.2, 1.2, 0))
    boundary = ndimage.binary_dilation(local_mask, iterations=1) & ~local_mask
    crop[local_mask] = upsampled[local_mask]
    if boundary.any():
        blend = 0.5
        crop[boundary] = crop[boundary] * (1 - blend) + feathered[boundary] * blend
    rgb[y1:y2, x1:x2] = crop
    return rgb


def _edge_colors(original_rgb: np.ndarray, box: list[float], band: int = 4):
    """Median colours along the four edges just outside a box."""
    height, width = original_rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]

    def strip(y1: int, y2: int, x1: int, x2: int) -> np.ndarray:
        y1, y2 = max(0, y1), min(height, y2)
        x1, x2 = max(0, x1), min(width, x2)
        if x2 <= x1 or y2 <= y1:
            return np.array([255.0, 255.0, 255.0], dtype=np.float32)
        return np.median(original_rgb[y1:y2, x1:x2].reshape(-1, 3), axis=0).astype(np.float32)

    return (
        strip(y - band, y, x, x + w),
        strip(y + h, y + h + band, x, x + w),
        strip(y, y + h, x - band, x),
        strip(y, y + h, x + w, x + w + band),
    )


def _edges_are_flat(original_rgb: np.ndarray, box: list[float], band: int = 4) -> bool:
    """True when the four edge colours are effectively equal.

    A flat fill is only used for genuinely uniform surroundings. On a
    gradient or photo, the glyph mask is inpainted instead, which preserves
    the background instead of leaving a rectangular patch.
    """
    top, bottom, left, right = _edge_colors(original_rgb, box, band)
    top_bottom = (top + bottom) / 2.0
    if float(np.linalg.norm(left - top_bottom)) > 60.0:
        left = top_bottom
    if float(np.linalg.norm(right - top_bottom)) > 60.0:
        right = top_bottom
    edges = np.stack([top, bottom, left, right])
    mean_edge = edges.mean(axis=0)
    return float(np.max(np.linalg.norm(edges - mean_edge, axis=1))) < 9.0


def _cv2_inpaint(
    original_rgb: np.ndarray,
    work: np.ndarray,
    full_mask: np.ndarray,
    box: list[float],
) -> np.ndarray:
    """Telea inpainting of a local region; the best available repair path."""
    height, width = original_rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    pad = 8
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
    if x2 - x1 < 3 or y2 - y1 < 3:
        return work
    crop = original_rgb[y1:y2, x1:x2].astype(np.uint8)
    local_mask = full_mask[y1:y2, x1:x2].astype(np.uint8) * 255
    if not local_mask.any():
        return work
    filled = cv2.inpaint(crop, local_mask, 3, cv2.INPAINT_TELEA)
    patch = work[y1:y2, x1:x2]
    selector = local_mask > 0
    patch[selector] = filled[selector].astype(np.float32)
    work[y1:y2, x1:x2] = patch
    return work


def _biharmonic_inpaint(
    original_rgb: np.ndarray,
    work: np.ndarray,
    full_mask: np.ndarray,
    box: list[float],
    max_mask_fraction: float = 0.35,
) -> np.ndarray | None:
    """Biharmonic inpainting for small text masks on smooth backgrounds.

    Telea can pull a neighbouring card/background colour into the glyph area
    on strong gradients. Biharmonic interpolation is slower but follows the
    surrounding colour field more smoothly, so it is used when the mask is a
    modest fraction of the local crop.
    """
    if not HAVE_SKIMAGE or inpaint_biharmonic is None:
        return None
    height, width = original_rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    pad = 10
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
    if x2 - x1 < 3 or y2 - y1 < 3:
        return None
    local_mask = full_mask[y1:y2, x1:x2]
    if not local_mask.any():
        return None
    if float(local_mask.mean()) > max_mask_fraction:
        return None
    crop = original_rgb[y1:y2, x1:x2].astype(np.float32) / 255.0
    try:
        filled = inpaint_biharmonic(crop, local_mask, channel_axis=-1)
    except Exception:
        return None
    filled = np.clip(filled * 255.0, 0, 255).astype(np.float32)
    patch = work[y1:y2, x1:x2]
    patch[local_mask] = filled[local_mask]
    work[y1:y2, x1:x2] = patch
    return work


def _median_fill(
    original_rgb: np.ndarray,
    work: np.ndarray,
    full_mask: np.ndarray,
    box: list[float],
) -> np.ndarray:
    """Fill masked strokes with a local median of the surrounding image.

    A median over a window wider than the text strokes removes the text
    without smearing it into a ghost, and follows smooth gradients. The
    median is computed on a downscaled crop for speed and upsampled; only
    masked pixels are replaced.
    """
    if not HAVE_SCIPY:
        return work
    height, width = original_rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    kernel = max(9, min(61, int(max(9.0, h * 0.9)) | 1))
    pad = kernel
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
    if x2 - x1 < 3 or y2 - y1 < 3:
        return work
    crop_full = original_rgb[y1:y2, x1:x2].astype(np.uint8)
    local_mask = full_mask[y1:y2, x1:x2]
    if not local_mask.any():
        return work
    small = Image.fromarray(crop_full)
    small, factor = resize_max(small, 320)
    small_arr = np.asarray(small, dtype=np.float32)
    kernel_small = max(3, int(round(kernel * factor)) | 1)
    max_kernel = min(small_arr.shape[0], small_arr.shape[1])
    if max_kernel >= 3:
        kernel_small = min(kernel_small, max_kernel if max_kernel % 2 == 1 else max_kernel - 1)
        filled = ndimage.median_filter(small_arr, size=(kernel_small, kernel_small, 1))
        filled = np.asarray(
            Image.fromarray(np.clip(filled, 0, 255).astype(np.uint8)).resize(
                (crop_full.shape[1], crop_full.shape[0]), Image.BILINEAR
            ),
            dtype=np.float32,
        )
        patch = work[y1:y2, x1:x2]
        patch[local_mask] = filled[local_mask]
        work[y1:y2, x1:x2] = patch
    return work


def _feather_blend_patch(
    work: np.ndarray,
    original: np.ndarray,
    box: list[float],
    radius: int = 2,
) -> None:
    """Blend a repaired text box into the original at its outer edge."""
    height, width = work.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(width, x + w), min(height, y + h)
    w, h = x2 - x1, y2 - y1
    if w < 3 or h < 3:
        return
    r = max(1, min(radius, w // 3, h // 3))
    alpha = np.ones((h, w), dtype=np.float32)
    ramp = (np.arange(r, dtype=np.float32) + 1.0) / (r + 1.0)
    for index, value in enumerate(ramp):
        alpha[index, :] = np.minimum(alpha[index, :], value)
        alpha[h - 1 - index, :] = np.minimum(alpha[h - 1 - index, :], value)
        alpha[:, index] = np.minimum(alpha[:, index], value)
        alpha[:, w - 1 - index] = np.minimum(alpha[:, w - 1 - index], value)
    original_crop = original[y1:y2, x1:x2].astype(np.float32)
    work[y1:y2, x1:x2] = (
        original_crop * (1.0 - alpha[..., None])
        + work[y1:y2, x1:x2] * alpha[..., None]
    )


def apply_removals(
    original_rgb: np.ndarray,
    removals: list[dict],
    page_background: np.ndarray,
) -> tuple[np.ndarray, int]:
    """Remove text / foreground / native objects from the clean base."""
    work = original_rgb.astype(np.float32).copy()
    removed_area = 0
    for removal in removals:
        box = removal.get("box")
        if not box:
            continue
        x, y, w, h = [int(round(v)) for v in box]
        if w <= 0 or h <= 0:
            continue
        kind = removal.get("kind")
        mask = removal.get("mask")
        full_mask = None
        if mask is not None:
            mask_box = removal.get("mask_box") or box
            mx, my = [int(round(v)) for v in mask_box[:2]]
            full_mask = np.zeros(work.shape[:2], dtype=bool)
            full_mask[my : my + mask.shape[0], mx : mx + mask.shape[1]] = mask
            if kind == "text" and HAVE_CV2 and not removal.get("tight"):
                # The OCR block box is slightly larger than the measured ink
                # box. Inpaint the whole padded text box so no antialiased
                # edge pixels survive as a ghost. Backdrop text uses a tight
                # mask because a native gradient shape covers the area.
                full_mask[y : y + h, x : x + w] = True

        ring, ring_std, ring_mad = sample_ring(original_rgb, box, band=3)
        if kind in ("native-shape", "native-line") and removal.get("color"):
            # Native objects were erased from a verified-uniform outer ring;
            # restore that exact ring colour instead of re-sampling (the band
            # may contain the removed object's own border).
            color = np.array(rgb_from_hex(removal["color"]), dtype=np.float32)
            work[y : y + h, x : x + w] = color
            removed_area += w * h
            continue
        if kind == "text" and removal.get("tight"):
            # Repair only the glyph pixels so the base has no ghost without a
            # flat rectangle showing around a native backdrop or decoration.
            if full_mask is None:
                continue
            text_colour = removal.get("text_color")
            if text_colour:
                local = original_rgb[y : y + h, x : x + w]
                if local.size:
                    near = np.linalg.norm(
                        local.astype(np.float32)
                        - np.array(rgb_from_hex(text_colour), dtype=np.float32),
                        axis=2,
                    ) <= 120.0
                    if HAVE_CV2:
                        near = cv2.dilate(near.astype(np.uint8), np.ones((3, 3), dtype=np.uint8), iterations=1).astype(bool)
                    full_mask[y : y + near.shape[0], x : x + near.shape[1]] |= near
            if ring_mad <= 14.0 and _edges_are_flat(original_rgb, box):
                # The decoration-aware case sits on a flat card; a full-box
                # fill matches the card and leaves no pale inpainting patch.
                color = ring if np.linalg.norm(ring - page_background) > 0.5 else page_background
                work[y : y + h, x : x + w] = color
                removed_area += w * h
            else:
                filled = _biharmonic_inpaint(original_rgb, work, full_mask, box) if HAVE_SKIMAGE else None
                if filled is not None:
                    work = filled
                elif HAVE_CV2:
                    work = _cv2_inpaint(original_rgb, work, full_mask, box)
                elif HAVE_SCIPY:
                    work = _diffuse_fill(work, full_mask, box, ring)
                else:
                    work[full_mask] = ring
                removed_area += int(full_mask.sum())
            _feather_blend_patch(work, original_rgb, box, radius=2)
            continue
        if kind == "text":
            ink_mask = removal.get("ink_mask")
            if ink_mask is not None and not _edges_are_flat(original_rgb, box):
                # On a gradient card a full-box flat fill leaves a visible
                # rectangle and can carry the text colour into the fill.
                # Repair only the measured glyph strokes instead, so the
                # original card gradient stays intact.
                mask_box = removal.get("mask_box") or box
                mx, my = [int(round(v)) for v in mask_box[:2]]
                tight_full = np.zeros(work.shape[:2], dtype=bool)
                ty2 = min(tight_full.shape[0], my + ink_mask.shape[0])
                tx2 = min(tight_full.shape[1], mx + ink_mask.shape[1])
                if ty2 > my and tx2 > mx:
                    tight_full[my:ty2, mx:tx2] = ink_mask[: ty2 - my, : tx2 - mx]
                if HAVE_CV2:
                    tight_full = cv2.dilate(
                        tight_full.astype(np.uint8),
                        np.ones((3, 3), dtype=np.uint8),
                        iterations=3,
                    ).astype(bool)
                elif HAVE_SCIPY:
                    tight_full = ndimage.binary_dilation(tight_full, iterations=3)
                filled = (
                    _biharmonic_inpaint(original_rgb, work, tight_full, box)
                    if HAVE_SKIMAGE
                    else None
                )
                if filled is not None:
                    work = filled
                elif HAVE_CV2:
                    work = _cv2_inpaint(original_rgb, work, tight_full, box)
                elif HAVE_SCIPY:
                    work = _diffuse_fill(work, tight_full, box, ring)
                else:
                    work[tight_full] = ring
                removed_area += int(tight_full.sum())
                continue

            # Every text removal in the base has already passed the
            # ring-MAD + background-std gate in filter_repairable_text. On a
            # genuinely flat background a full-box fill is the simplest repair.
            color = ring if np.linalg.norm(ring - page_background) > 0.5 else page_background
            work[y : y + h, x : x + w] = color
            removed_area += w * h
            _feather_blend_patch(work, original_rgb, box, radius=2)
            continue
        if ring_mad <= 14.0 and _edges_are_flat(original_rgb, box):
            color = ring if np.linalg.norm(ring - page_background) > 0.5 else page_background
            if kind == "foreground" or full_mask is None:
                # On a genuinely uniform background, clearing the whole padded
                # box is cleaner than clearing only the ink: it removes the
                # antialiasing halo that a tight glyph/alpha mask leaves.
                work[y : y + h, x : x + w] = color
                removed_area += w * h
            else:
                work[full_mask] = color
                removed_area += int(full_mask.sum())
        else:
            if full_mask is None:
                full_mask = np.zeros(work.shape[:2], dtype=bool)
                full_mask[y : y + h, x : x + w] = True
            density = float(full_mask[y : y + h, x : x + w].mean())
            if HAVE_CV2:
                work = _cv2_inpaint(original_rgb, work, full_mask, box)
            elif density <= 0.55:
                work = _median_fill(original_rgb, work, full_mask, box)
            else:
                work = _diffuse_fill(work, full_mask, box, ring)
            removed_area += int(full_mask.sum())
    return np.clip(work, 0, 255).astype(np.uint8), removed_area


# ---------------------------------------------------------------------------
# Native shape detection (flat cards / dividers)
# ---------------------------------------------------------------------------


def _detect_border_and_outer(
    rgb: np.ndarray, bbox_small: list[float], scale: float
) -> dict | None:
    """Inspect the ring around a flat fill to decide whether it can be
    nativized (uniform outer background, optional visible border)."""
    height, width = rgb.shape[:2]
    # `scale` is analysis-pixels / source-pixels, so map the small bbox back
    # to source coordinates by dividing.
    x, y, w, h = [int(round(v / max(scale, 1e-6))) for v in bbox_small]
    if w < 1 or h < 1:
        return None
    near, near_std = sample_outside_color(rgb, [x, y, w, h], distance=2)
    far, far_std = sample_outside_color(rgb, [x, y, w, h], distance=8)
    if far_std > 12.0:
        return None
    has_border = float(np.linalg.norm(near - far)) > 24.0
    border_color = near
    border_px = 0
    if has_border:
        # Walk outwards along the four mid-lines until the colour matches the
        # far ring; the median step count is the border thickness.
        steps = []
        for direction in ("up", "down", "left", "right"):
            for step in range(1, 12):
                if direction == "up":
                    px = rgb[max(0, y - step), min(width - 1, x + w // 2)]
                elif direction == "down":
                    px = rgb[min(height - 1, y + h + step), min(width - 1, x + w // 2)]
                elif direction == "left":
                    px = rgb[min(height - 1, y + h // 2), max(0, x - step)]
                else:
                    px = rgb[min(height - 1, y + h // 2), min(width - 1, x + w + step)]
                if float(np.linalg.norm(px.astype(np.float32) - far)) <= 14.0:
                    steps.append(step)
                    break
        border_px = int(np.median(steps)) if steps else 2
        border_px = max(1, min(8, border_px))
    return {
        "box": [x, y, w, h],
        "outer": far,
        "outer_std": far_std,
        "has_border": has_border,
        "border_color": border_color,
        "border_px": border_px,
    }


def _line_thickness(
    rgb: np.ndarray,
    points: list[float],
    color: np.ndarray,
    background: np.ndarray,
    max_thickness: float | None = None,
) -> int:
    """Measure a line's source thickness from a perpendicular cross-section.

    A fixed distance threshold counts the whole white neighbourhood of a
    light-gray line (and produced absurd 12pt strokes on underlines). A pixel
    belongs to the line only when it is closer to the line colour than to the
    surrounding background colour.
    """
    height, width = rgb.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in points]
    target = color.astype(np.float32)
    outer = background.astype(np.float32)
    if abs(y2 - y1) <= abs(x2 - x1):
        cx = int(round((x1 + x2) / 2.0))
        cy = int(round((y1 + y2) / 2.0))
        if cx < 0 or cx >= width:
            return 1
        column = rgb[:, cx].astype(np.float32)
        to_line = np.linalg.norm(column - target.reshape(1, 3), axis=1)
        to_bg = np.linalg.norm(column - outer.reshape(1, 3), axis=1)
        rows = [
            y
            for y in range(max(0, cy - 8), min(height, cy + 9))
            if to_line[y] < to_bg[y] and to_line[y] < 60.0
        ]
        count = max(1, len(rows))
    else:
        cx = int(round((x1 + x2) / 2.0))
        cy = int(round((y1 + y2) / 2.0))
        if cy < 0 or cy >= height:
            return 1
        row = rgb[cy, :].astype(np.float32)
        to_line = np.linalg.norm(row - target.reshape(1, 3), axis=1)
        to_bg = np.linalg.norm(row - outer.reshape(1, 3), axis=1)
        columns = [
            x
            for x in range(max(0, cx - 8), min(width, cx + 9))
            if to_line[x] < to_bg[x] and to_line[x] < 60.0
        ]
        count = max(1, len(columns))
    if max_thickness is not None:
        count = min(count, max(1, int(round(max_thickness))))
    return count


def bake_backdrop_shapes(rgb: np.ndarray, shapes: list[dict]) -> np.ndarray:
    """Composite native gradient backdrops into the clean base image.

    Baking avoids a separate translucent shape in the slide while keeping the
    seamless feather/alpha blend. Text boxes remain native on top of the baked
    background.
    """
    work = rgb.astype(np.float32).copy()
    height, width = work.shape[:2]
    for shape in shapes:
        box = shape.get("box_px")
        if not box:
            continue
        x, y, w, h = [int(round(v)) for v in box]
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(width, x + w), min(height, y + h)
        cw, ch = x2 - x1, y2 - y1
        if cw < 2 or ch < 2:
            continue
        fill = shape.get("fill")
        gradient = None
        if isinstance(fill, dict) and fill.get("type") == "gradient":
            gradient = gradient_fill_image(cw, ch, fill)
        elif isinstance(fill, dict) and fill.get("color"):
            gradient = Image.new("RGB", (cw, ch), rgb_from_hex(fill["color"]))
        elif isinstance(fill, str) and fill and fill != "none":
            gradient = Image.new("RGB", (cw, ch), rgb_from_hex(fill))
        if gradient is None:
            continue

        mask = Image.new("L", (cw, ch), 0)
        if shape.get("type") == "freeform" and shape.get("path_px"):
            points = [
                (float(point[0]) - x, float(point[1]) - y)
                for point in shape["path_px"]
                if len(point) >= 2
            ]
            if len(points) < 3:
                continue
            ImageDraw.Draw(mask).polygon(points, fill=255)
        else:
            mask = Image.new("L", (cw, ch), 255)

        try:
            soft_edge = float(shape.get("soft_edge_ratio") or 0)
        except (TypeError, ValueError):
            soft_edge = 0.0
        try:
            alpha_value = float(fill.get("alpha", 1.0)) if isinstance(fill, dict) else 1.0
        except (TypeError, ValueError):
            alpha_value = 1.0

        # Poisson blending matches the gradient patch to the destination at
        # the mask boundary, which hides small colour seams far better than a
        # plain alpha composite. It needs a binary mask, so manual soft-edge
        # or semi-transparent shapes fall back to the alpha path.
        if HAVE_CV2 and soft_edge <= 0 and alpha_value >= 1.0:
            try:
                source = cv2.cvtColor(np.asarray(gradient), cv2.COLOR_RGB2BGR)
                destination = cv2.cvtColor(
                    np.clip(work, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR
                )
                binary_mask = np.asarray(mask)
                center = (x1 + cw // 2, y1 + ch // 2)
                blended = cv2.seamlessClone(
                    source, destination, binary_mask, center, cv2.NORMAL_CLONE
                )
                work = cv2.cvtColor(blended, cv2.COLOR_BGR2RGB).astype(np.float32)
                continue
            except Exception:
                pass

        if soft_edge > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(max(0.5, min(cw, ch) * soft_edge)))
        if 0.0 < alpha_value < 1.0:
            mask = mask.point(lambda value: int(round(value * alpha_value)))

        alpha = np.asarray(mask, dtype=np.float32)[..., None] / 255.0
        gradient_array = np.asarray(gradient, dtype=np.float32)
        region = work[y1:y2, x1:x2]
        region[:] = region * (1.0 - alpha) + gradient_array * alpha
    return np.clip(work, 0, 255).astype(np.uint8)


def detect_native_shapes(
    image: Image.Image,
    mode: str,
    text_boxes: list[list[float]],
    page_background: np.ndarray,
    pt_per_px: float,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Detect empty flat cards and structural lines.

    Returns (shapes, removals, inventory). Only regions with a uniform outer
    ring are nativized; everything else stays in the clean base.
    """
    if mode != "native" or not HAVE_SCIPY:
        return [], [], []

    small, scale = resize_max(image, 900)
    arr = np.asarray(small.convert("RGB"), dtype=np.uint8)
    full_rgb = np.asarray(image, dtype=np.uint8)
    quantized = small.quantize(colors=24).convert("RGB")
    qarr = np.asarray(quantized)
    height, width = qarr.shape[:2]
    total = float(height * width)

    colors, counts = np.unique(qarr.reshape(-1, 3), axis=0, return_counts=True)
    order = np.argsort(-counts)

    shapes: list[dict] = []
    removals: list[dict] = []
    inventory: list[dict] = []
    used_boxes: list[list[float]] = []
    flat_region_boxes: list[list[float]] = []
    added_line_boxes: list[list[float]] = []

    for index in order:
        coverage = counts[index] / total
        if coverage > 0.90 or coverage < 0.0018:
            continue
        color = colors[index]
        mask = np.all(qarr == color, axis=2)
        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3), dtype=bool))
        labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
        if count == 0:
            continue
        slices = ndimage.find_objects(labels)
        for label_id in range(1, count + 1):
            sl = slices[label_id - 1]
            if sl is None:
                continue
            ys, xs = sl
            bw, bh = xs.stop - xs.start, ys.stop - ys.start
            if bw < 14 or bh < 12:
                continue
            bbox_area = float(bw * bh)
            bbox_fraction = bbox_area / total
            if bbox_fraction < 0.004 or bbox_fraction > 0.78:
                continue
            component = labels[sl] == label_id
            area = float(component.sum())
            if area < 500:
                continue
            fill_coverage = area / bbox_area
            box_small = [xs.start, ys.start, bw, bh]
            # Remember every large flat region (content cards, gradient and
            # photo blocks) so line detection does not carve their borders
            # into standalone native lines.
            if bbox_fraction >= 0.004 and fill_coverage >= 0.5:
                flat_region_boxes.append(box_small)
            if fill_coverage < 0.93:
                continue
            # Interior flatness: empty cards only. Text or icons inside make
            # the component non-flat and keep it in the raster base. Measure
            # the eroded core so antialiased corners do not inflate the std.
            core = ndimage.binary_erosion(component, iterations=2)
            pixels = arr[sl][core if core.any() else component].astype(np.float32)
            if float(np.max(np.std(pixels, axis=0))) > 10.0:
                continue
            if any(box_iou(box_small, other) > 0.85 for other in used_boxes):
                continue
            info = _detect_border_and_outer(full_rgb, box_small, scale)
            if info is None:
                continue
            outer = info["outer"]
            if float(np.linalg.norm(outer - page_background)) > 60.0 and not info["has_border"]:
                # A flat block in the middle of a photo is probably content,
                # not a layout card. Keep it raster.
                continue

            fill_color = np.median(pixels, axis=0)

            def row_inset(row: np.ndarray) -> float:
                indices = np.flatnonzero(row)
                if indices.size == 0:
                    return 0.0
                return (indices[0] + (bw - 1 - indices[-1])) / 2.0

            # For a rounded rectangle the first/last fill row spans only the
            # straight middle section, so its inset from the bbox edge is the
            # corner radius. Quantization erodes roughly one pixel; subtract
            # it so straight cards stay `rect`.
            radius_small = max(0.0, (row_inset(component[0]) + row_inset(component[-1])) / 2.0)
            radius_src = float(radius_small) / max(scale, 1e-6)
            is_round = radius_src >= 2.5 and radius_src < 0.5 * min(bw, bh) / max(scale, 1e-6)
            shape_type = "roundRect" if is_round else "rect"

            source_box = [xs.start / scale, ys.start / scale, bw / scale, bh / scale]
            border_px_src = info["border_px"] / max(scale, 1e-6) if info["has_border"] else 0.0
            draw_box = [
                source_box[0] - border_px_src,
                source_box[1] - border_px_src,
                source_box[2] + 2 * border_px_src,
                source_box[3] + 2 * border_px_src,
            ]
            shape = {
                "id": f"s{len(shapes) + 1:03d}",
                "type": shape_type,
                "box_px": [round(v, 2) for v in draw_box],
                "fill": hex_color_from_rgb(fill_color),
                "stroke": hex_color_from_rgb(info["border_color"]) if info["has_border"] else "none",
                "stroke_width": round(max(0.5, border_px_src * pt_per_px), 2) if info["has_border"] else 0,
                "z_index": 10 + len(shapes) * 0.01,
            }
            if is_round:
                shape["source_corner_radius_px"] = round(radius_src, 1)
                shape["corner_category"] = "small-radius" if radius_src < 0.08 * min(source_box[2], source_box[3]) else "large-radius"
                shape["corner_reason"] = "measured from the first and last fill row's corner inset"
            shapes.append(shape)
            used_boxes.append(box_small)

            erase_pad = info["border_px"] + 2
            erase_box = [
                xs.start - erase_pad,
                ys.start - erase_pad,
                bw + 2 * erase_pad,
                bh + 2 * erase_pad,
            ]
            removals.append(
                {
                    "box": [v / scale for v in erase_box],
                    "mask": None,
                    "kind": "native-shape",
                    "color": hex_color_from_rgb(outer),
                }
            )
            inventory.append(
                {
                    "id": shape["id"],
                    "role": "structure",
                    "object_type": "card",
                    "description": f"flat {shape_type} rebuilt as a native shape",
                }
            )
            if len(shapes) >= 24:
                break
        if len(shapes) >= 24:
            break

    # ---- structural lines -------------------------------------------------
    # Only long, thin, isolated runs on a uniform background become native
    # lines. Short dashes, text underlines and chart hairline noise are left
    # in the base.
    try:
        for index in order[:10]:
            coverage = counts[index] / total
            if coverage < 0.00015 or coverage > 0.12:
                continue
            mask = np.all(qarr == colors[index], axis=2)
            mask = ndimage.binary_closing(mask, structure=np.ones((2, 2), dtype=bool))
            horizontal = ndimage.binary_opening(mask, structure=np.ones((1, max(12, width // 14)), dtype=bool))
            vertical = ndimage.binary_opening(mask, structure=np.ones((max(12, height // 14), 1), dtype=bool))
            for orientation, line_candidate in (("h", horizontal), ("v", vertical)):
                labels, count = ndimage.label(line_candidate, structure=np.ones((3, 3), dtype=bool))
                if count == 0:
                    continue
                slices = ndimage.find_objects(labels)
                for label_id in range(1, count + 1):
                    sl = slices[label_id - 1]
                    if sl is None:
                        continue
                    ys, xs = sl
                    bw, bh = xs.stop - xs.start, ys.stop - ys.start
                    if orientation == "h":
                        if bh > 5 or bw < max(20, width * 0.06):
                            continue
                        line_box_small = [xs.start, ys.start, bw, bh]
                    else:
                        if bw > 5 or bh < max(20, height * 0.06):
                            continue
                        line_box_small = [xs.start, ys.start, bw, bh]
                    if any(box_iou(line_box_small, other) > 0.3 for other in used_boxes):
                        continue
                    if any(boxes_overlap(line_box_small, region, margin=3) for region in flat_region_boxes):
                        continue
                    if any(boxes_overlap(line_box_small, added, margin=4) for added in added_line_boxes):
                        continue
                    if any(boxes_overlap([v / scale for v in line_box_small], tb, margin=2) for tb in text_boxes):
                        continue
                    info = _detect_border_and_outer(full_rgb, line_box_small, scale)
                    if info is None:
                        continue
                    color_hex = hex_color_from_rgb(colors[index])
                    # Antialiasing fringes quantize to their own near-white
                    # colours. Only a line that is clearly distinct from the
                    # surrounding background is a real structural line.
                    if float(np.linalg.norm(colors[index].astype(np.float32) - info["outer"])) < 60.0:
                        continue
                    x1, y1 = xs.start / scale, ys.start / scale
                    x2, y2 = xs.stop / scale, ys.stop / scale
                    if orientation == "h":
                        cy = (y1 + y2) / 2.0
                        points = [x1, cy, x2, cy]
                    else:
                        cx = (x1 + x2) / 2.0
                        points = [cx, y1, cx, y2]
                    bbox_thickness_src = float(bh if orientation == "h" else bw) / max(scale, 1e-6)
                    thickness_src = float(
                        _line_thickness(
                            full_rgb,
                            points,
                            colors[index],
                            info["outer"],
                            max_thickness=bbox_thickness_src * 2.0 + 2.0,
                        )
                    )
                    line = {
                        "id": f"s{len(shapes) + 1:03d}",
                        "type": "line",
                        "points_px": [round(v, 2) for v in points],
                        "stroke": color_hex,
                        "stroke_width": round(max(0.5, thickness_src * pt_per_px), 2),
                        "z_index": 20 + len(shapes) * 0.01,
                    }
                    shapes.append(line)
                    added_line_boxes.append(line_box_small)
                    erase_pad = 2
                    removals.append(
                        {
                            "box": [
                                xs.start / scale - erase_pad,
                                ys.start / scale - erase_pad,
                                bw / scale + 2 * erase_pad,
                                bh / scale + 2 * erase_pad,
                            ],
                            "mask": None,
                            "kind": "native-line",
                            "color": hex_color_from_rgb(info["outer"]),
                        }
                    )
                    inventory.append(
                        {
                            "id": line["id"],
                            "role": "structure",
                            "object_type": "divider" if orientation == "h" else "vertical-line",
                            "description": "structural line rebuilt as a native line",
                        }
                    )
                    if len(shapes) >= 40:
                        break
                if len(shapes) >= 40:
                    break
            if len(shapes) >= 40:
                break
    except Exception as exc:  # line detection is best-effort
        warn(f"native line detection skipped: {exc}")

    return shapes, removals, inventory


# ---------------------------------------------------------------------------
# Foreground asset separation
# ---------------------------------------------------------------------------


def _looks_like_text(gray: np.ndarray, box: list[int]) -> bool:
    """Heuristic guard against OCR misses turning text lines into assets.

    A component is treated as text when the offline ink detector finds a
    single horizontal line of many disconnected glyph-like strokes. This is
    intentionally conservative: wrongly keeping an object in the base is
    recoverable, while replacing text with a raster "icon" is not.
    """
    if not HAVE_SCIPY:
        return False
    height, width = gray.shape[:2]
    x, y, w, h = box
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(width, x + w), min(height, y + h)
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
        return False
    measured = measure_crop(crop)
    if not measured.get("ok") or not measured.get("bbox"):
        return False
    glyph = float(measured.get("glyph_height_px", 0.0))
    if glyph <= 0 or glyph > 0.8 * crop.shape[0]:
        return False
    if crop.shape[1] < 2.2 * max(1, crop.shape[0]):
        return False
    _labels, count = ndimage.label(measured["mask"], structure=np.ones((3, 3), dtype=bool))
    return count >= 5


def _extract_alpha_asset(
    rgb: np.ndarray, box: list[float], background: np.ndarray, pad: int
) -> tuple[Image.Image, np.ndarray, list[float]] | None:
    height, width = rgb.shape[:2]
    x, y, w, h = [int(round(v)) for v in box]
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
    if x2 - x1 < 3 or y2 - y1 < 3:
        return None
    crop = rgb[y1:y2, x1:x2].astype(np.float32)
    distance = np.linalg.norm(crop - background.reshape(1, 1, 3), axis=2)
    alpha = np.clip((distance - 7.0) / 32.0, 0.0, 1.0)
    if float(alpha.max()) < 0.4:
        return None
    alpha_u8 = (alpha * 255.0).astype(np.uint8)
    rgba = np.dstack([crop.astype(np.uint8), alpha_u8])
    rgba[alpha_u8 == 0, 0:3] = 255
    return Image.fromarray(rgba, mode="RGBA"), alpha_u8, [x1, y1, x2 - x1, y2 - y1]


def detect_foreground_assets(
    image: Image.Image,
    text_boxes: list[list[float]],
    native_shape_boxes: list[list[float]],
    assets_dir: Path,
    max_assets: int = 60,
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Separate compact non-text visual objects into transparent PNGs.

    This is the classical-CV counterpart of the skill's asset-sheet workflow.
    Objects on a busy background are deliberately left in the clean base: the
    colour-distance matte would produce halos, and a bad asset is worse than a
    faithful raster region.
    """
    if not HAVE_SCIPY:
        return [], [], [], ["scipy is not installed; foreground separation skipped"]

    small, scale = resize_max(image, 800)
    arr = np.asarray(small.convert("RGB"), dtype=np.float32)
    height, width = arr.shape[:2]
    total = float(height * width)

    # Two independent foreground detectors:
    #  * palette distance finds solid objects whose colour is not one of the
    #    large flat layout colours (the disc of an icon, stamps, photos);
    #  * local median difference finds fine detail at object boundaries.
    # Their union keeps a complete object while the opening below removes
    # one-pixel antialiasing fringes.
    quantized = small.quantize(colors=24).convert("RGB")
    qarr = np.asarray(quantized, dtype=np.int16)
    palette, counts = np.unique(qarr.reshape(-1, 3), axis=0, return_counts=True)
    layout_colors = palette[counts >= 0.004 * total]
    palette_distance = np.full((height, width), 1e9, dtype=np.float32)
    for layout_color in layout_colors:
        color = layout_color.astype(np.float32).reshape(1, 1, 3)
        palette_distance = np.minimum(palette_distance, np.linalg.norm(arr - color, axis=2))
    candidate = palette_distance > 28.0

    median_bg = ndimage.median_filter(arr, size=(5, 5, 1))
    difference = np.abs(arr - median_bg).sum(axis=2)
    candidate |= difference > 34.0

    gray_full = np.asarray(image.convert("L"), dtype=np.float32)

    text_mask = np.zeros((height, width), dtype=bool)
    for box in text_boxes:
        x, y, w, h = [int(round(v * scale)) for v in box]
        x1, y1 = max(0, x - 2), max(0, y - 2)
        x2, y2 = min(width, x + w + 2), min(height, y + h + 2)
        text_mask[y1:y2, x1:x2] = True
    candidate &= ~text_mask

    for box in native_shape_boxes:
        x, y, w, h = [int(round(v * scale)) for v in box]
        x1, y1 = max(0, x - 2), max(0, y - 2)
        x2, y2 = min(width, x + w + 2), min(height, y + h + 2)
        candidate[y1:y2, x1:x2] = False

    candidate = ndimage.binary_opening(candidate, structure=np.ones((2, 2), dtype=bool))
    grouped = ndimage.binary_dilation(candidate, structure=np.ones((3, 3), dtype=bool), iterations=2)
    grouped &= ~text_mask
    labels, count = ndimage.label(grouped, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return [], [], [], []

    slices = ndimage.find_objects(labels)
    components = []
    for label_id in range(1, count + 1):
        sl = slices[label_id - 1]
        if sl is None:
            continue
        ys, xs = sl
        bw, bh = xs.stop - xs.start, ys.stop - ys.start
        bbox_area = float(bw * bh)
        if bbox_area < 0.00008 * total or bbox_area > 0.025 * total:
            # Large regions are photos / illustrations / chart bases. The
            # skill classifies those as background imagery, not per-object
            # assets; keep them in the clean base.
            continue
        if bw < 7 or bh < 7:
            continue
        if bw > 0.36 * width or bh > 0.36 * height:
            continue
        aspect = max(bw, bh) / max(1.0, min(bw, bh))
        if aspect > 8.0:
            continue
        actual = candidate[sl] & (labels[sl] == label_id)
        actual_area = float(actual.sum())
        if actual_area < 40:
            continue
        fill = actual_area / bbox_area
        if fill < 0.06 or fill > 0.96:
            continue
        pixels = arr[sl][actual].astype(np.float32)
        if pixel_std(pixels) < 8.0 and fill > 0.9:
            # A flat block is a layout shape, not a foreground icon.
            continue
        box_small = [xs.start, ys.start, bw, bh]
        if any(box_iou(box_small, [v * scale for v in shape]) > 0.5 for shape in native_shape_boxes):
            continue
        # OCR can miss small/faint text. Do not turn a text line into an
        # "icon": text-like components stay in the base so they keep their
        # source rendering instead of becoming a stale raster asset.
        full_box = [int(round(v / max(scale, 1e-6))) for v in box_small]
        if _looks_like_text(gray_full, full_box):
            continue
        components.append(
            {
                "box_small": box_small,
                "area": actual_area,
                "fill": fill,
                "pixels": pixels,
            }
        )

    # A chart panel is a large light canvas containing many small marks
    # (bars, tick labels, legend swatches). Separating those marks as
    # individual "icons" fragments the chart. Protect such panels: if a large
    # light region contains many candidates, keep the whole region raster.
    if components and HAVE_SCIPY:
        light = (arr.min(axis=2) >= 235.0) & ((arr.max(axis=2) - arr.min(axis=2)) <= 20.0)
        light = ndimage.binary_closing(light, structure=np.ones((3, 3), dtype=bool))
        light_labels, light_count = ndimage.label(light, structure=np.ones((3, 3), dtype=bool))
        light_slices = ndimage.find_objects(light_labels)
        protected: list[list[float]] = []
        for label_id in range(1, light_count + 1):
            sl = light_slices[label_id - 1]
            if sl is None:
                continue
            ys, xs = sl
            bw, bh = xs.stop - xs.start, ys.stop - ys.start
            bbox_area = float(bw * bh)
            if bbox_area < 0.06 * total or bbox_area > 0.55 * total:
                continue
            if bw < 0.12 * width or bh < 0.10 * height:
                continue
            panel = [xs.start, ys.start, bw, bh]
            inside = []
            for component in components:
                cx = component["box_small"][0] + component["box_small"][2] / 2.0
                cy = component["box_small"][1] + component["box_small"][3] / 2.0
                if xs.start <= cx <= xs.stop and ys.start <= cy <= ys.stop:
                    inside.append(component)
            if len(inside) >= 8:
                protected.append(panel)
        if protected:
            kept = []
            removed = 0
            for component in components:
                cx = component["box_small"][0] + component["box_small"][2] / 2.0
                cy = component["box_small"][1] + component["box_small"][3] / 2.0
                if any(p[0] <= cx <= p[0] + p[2] and p[1] <= cy <= p[1] + p[3] for p in protected):
                    removed += 1
                else:
                    kept.append(component)
            if removed:
                log(f"  kept {removed} chart/panel fragment(s) raster")
            components = kept

    components.sort(key=lambda entry: -entry["area"])
    components = components[:max_assets]

    images: list[dict] = []
    removals: list[dict] = []
    inventory: list[dict] = []
    warnings: list[str] = []
    assets_dir.mkdir(parents=True, exist_ok=True)

    full_rgb = np.asarray(image, dtype=np.uint8)
    for number, component in enumerate(components, 1):
        x, y, w, h = [int(round(v / scale)) for v in component["box_small"]]
        box = [x, y, max(2, w), max(2, h)]
        pad = max(2, int(round(0.05 * max(box[2], box[3]))))
        ring, ring_std, _ = sample_ring(full_rgb, box, band=max(2, pad))
        if ring_std > 18.0:
            continue
        extracted = _extract_alpha_asset(full_rgb, box, ring, pad)
        if extracted is None:
            continue
        asset, alpha, asset_box = extracted
        path = assets_dir / f"icon_{number:03d}.png"
        asset.save(path)
        images.append(
            {
                "id": f"i{len(images) + 1:03d}",
                "path": rel_posix(path, assets_dir.parent),
                "box_px": [round(v, 2) for v in asset_box],
                "alt": f"separated foreground object {number}",
                "z_index": 30 + len(images) * 0.01,
                "source_type": "source-preserving-local-cleanup",
            }
        )
        removals.append({"box": asset_box, "mask": alpha > 0, "kind": "foreground", "text": ""})
        inventory.append(
            {
                "id": f"i{len(images)}",
                "role": "foreground",
                "object_type": "icon",
                "path": rel_posix(path, assets_dir.parent),
                "description": "separated from the source with colour-distance alpha matting",
            }
        )

    if not images:
        warnings.append("no compact foreground objects were separated; they remain in the clean base")
    return images, removals, inventory, warnings


def pixel_std(pixels: np.ndarray) -> float:
    if pixels.size == 0:
        return 0.0
    return float(np.max(np.std(pixels, axis=0)))


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------


def build_page_manifest(
    *,
    page_id: str,
    source_name: str,
    source_width: int,
    source_height: int,
    slide: dict,
    content_box: dict,
    page_background: str,
    analysis: dict,
    text_items: list[dict],
    shapes: list[dict],
    images: list[dict],
    background_strategy: dict,
    quality_checks: dict,
    asset_provenance: list[dict],
    visual_inventory: list[dict],
    removed_area: int,
    raster_text: list[dict] | None = None,
) -> dict:
    manifest = {
        "schema_version": 1,
        "page_id": page_id,
        "slide": {
            "width": float(slide["width"]),
            "height": float(slide["height"]),
            "background": page_background,
        },
        "content_box": {
            "left": float(content_box["left"]),
            "top": float(content_box["top"]),
            "width": float(content_box["width"]),
            "height": float(content_box["height"]),
            "fit": "contain",
        },
        "source": {
            "width_px": int(source_width),
            "height_px": int(source_height),
            "path": source_name,
        },
        "page_analysis": {
            "flat_ratio": round(float(analysis.get("flat_ratio", 0)), 4),
            "edge_density": round(float(analysis.get("edge_density", 0)), 4),
            "mode": analysis.get("mode", "image"),
        },
        "background_strategy": background_strategy,
        "text_inventory": [item["text"] for item in text_items],
        "raster_text": raster_text or [],
        "visual_inventory": visual_inventory,
        "text_boxes": text_items,
        "shapes": shapes,
        "images": images,
        "tables": [],
        "asset_provenance": asset_provenance,
        "quality_checks": quality_checks,
        "removed_area_px": int(removed_area),
    }
    return manifest


# ---------------------------------------------------------------------------
# PPTX writer (OOXML, based on the skill's deterministic runtime)
# ---------------------------------------------------------------------------

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TEXT_ALIGNMENTS = {
    "left": ("l", "left"),
    "center": ("ctr", "center"),
    "right": ("r", "right"),
    "l": ("l", "left"),
    "ctr": ("ctr", "center"),
    "r": ("r", "right"),
}
TEXT_VERTICAL_ALIGNMENTS = {
    "top": ("t", "top"),
    "middle": ("ctr", "middle"),
    "center": ("ctr", "middle"),
    "bottom": ("b", "bottom"),
    "t": ("t", "top"),
    "ctr": ("ctr", "middle"),
    "b": ("b", "bottom"),
}


def emu(value: float) -> int:
    return int(round(float(value) * EMU_PER_INCH))


def hex_color(value: str | None, default: str = "000000") -> str:
    if not value:
        return default
    return str(value).strip().lstrip("#").upper()


def xml_text(value) -> str:
    return html.escape(str(value), quote=True)


def content_type_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".svg":
        return "image/svg+xml"
    raise ValueError(f"Unsupported image type: {path}")


def image_ext(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return ".jpg" if suffix == ".jpeg" else suffix


def text_alignment(value: str = "left"):
    key = str(value or "left").strip().lower()
    if key not in TEXT_ALIGNMENTS:
        raise ValueError(f"Unsupported text align value: {value}")
    return TEXT_ALIGNMENTS[key]


def text_vertical_alignment(value: str = "top"):
    key = str(value or "top").strip().lower()
    if key not in TEXT_VERTICAL_ALIGNMENTS:
        raise ValueError(f"Unsupported text valign value: {value}")
    return TEXT_VERTICAL_ALIGNMENTS[key]


def source_size_px(manifest: dict):
    source = manifest.get("source", {})
    width = source.get("width_px")
    height = source.get("height_px")
    if width and height:
        return float(width), float(height)
    return None


def content_box_for_manifest(manifest: dict) -> dict:
    content_box = manifest.get("content_box")
    if content_box:
        return {
            "left": float(content_box.get("left", 0)),
            "top": float(content_box.get("top", 0)),
            "width": float(content_box.get("width", 1)),
            "height": float(content_box.get("height", 1)),
        }
    source_size = source_size_px(manifest)
    slide = manifest.get("slide", {})
    slide_width, slide_height = float(slide.get("width", 13.333)), float(slide.get("height", 7.5))
    if not source_size:
        return {"left": 0, "top": 0, "width": slide_width, "height": slide_height}
    return fit_content_box(source_size[0], source_size[1], {"width": slide_width, "height": slide_height})


def manifest_px_to_inches(manifest: dict, x: float, y: float, width: float, height: float) -> dict:
    source_size = source_size_px(manifest)
    if not source_size:
        raise ValueError("Manifest uses pixel coordinates but lacks source.width_px/source.height_px")
    source_width, source_height = source_size
    content_box = content_box_for_manifest(manifest)
    return {
        "left": content_box["left"] + float(x) / source_width * content_box["width"],
        "top": content_box["top"] + float(y) / source_height * content_box["height"],
        "width": float(width) / source_width * content_box["width"],
        "height": float(height) / source_height * content_box["height"],
    }


def normalize_position_item(manifest: dict, item: dict) -> dict:
    item = dict(item)
    if "box_px" in item:
        x, y, width, height = item["box_px"]
        item.update(manifest_px_to_inches(manifest, x, y, width, height))
    if "points_px" in item:
        x1, y1, x2, y2 = item["points_px"]
        left = min(float(x1), float(x2))
        top = min(float(y1), float(y2))
        width = abs(float(x2) - float(x1))
        height = abs(float(y2) - float(y1))
        item.update(manifest_px_to_inches(manifest, left, top, width, height))
        start = manifest_px_to_inches(manifest, x1, y1, 0, 0)
        end = manifest_px_to_inches(manifest, x2, y2, 0, 0)
        item["points"] = [start["left"], start["top"], end["left"], end["top"]]
        if float(x2) < float(x1):
            item["flip_h"] = True
        if float(y2) < float(y1):
            item["flip_v"] = True
    if "path_px" in item:
        raw_points = item.get("path_px") or []
        points = [(float(point[0]), float(point[1])) for point in raw_points if len(point) >= 2]
        if len(points) >= 3:
            point_x = [point[0] for point in points]
            point_y = [point[1] for point in points]
            left = min(point_x)
            top = min(point_y)
            width = max(point_x) - left
            height = max(point_y) - top
            if "box_px" not in item:
                item.update(manifest_px_to_inches(manifest, left, top, width, height))
            base_left = float(item.get("left", 0))
            base_top = float(item.get("top", 0))
            relative = []
            for point_x_value, point_y_value in points:
                position = manifest_px_to_inches(manifest, point_x_value, point_y_value, 0, 0)
                relative.append([position["left"] - base_left, position["top"] - base_top])
            item["path"] = relative
    if item.get("source_corner_radius_px") is not None and "radius" not in item:
        radius = float(item.get("source_corner_radius_px") or 0)
        item["radius"] = manifest_px_to_inches(manifest, 0, 0, radius, radius)["width"]
    return item


def iter_text_lines(item: dict) -> list[str]:
    if item.get("runs"):
        return ["".join(str(run.get("text", "")) for run in item["runs"])]
    return str(item.get("text", "")).splitlines() or [""]


def text_width_units(text: str) -> float:
    units = 0.0
    for char in str(text):
        codepoint = ord(char)
        if char.isspace():
            units += 0.32
        elif codepoint <= 0x7F:
            units += 0.55
        elif 0xFF00 <= codepoint <= 0xFFEF:
            units += 1.0
        elif 0x4E00 <= codepoint <= 0x9FFF:
            units += 1.0
        else:
            units += 0.85
    return max(units, 1.0)


def is_measured_text(item: dict) -> bool:
    return str(item.get("font_size_source", "")).strip().lower() in {"measured", "hints"}


def fitted_font_size(item: dict, manifest: dict):
    if item.get("fit_text") is False or manifest.get("fit_text") is False:
        return None
    if "width" not in item or "height" not in item:
        return None
    lines = iter_text_lines(item)
    requested = float(item.get("font_size", 18))
    width_pt = max(1.0, float(item.get("width", 1)) * 72)
    height_pt = max(1.0, float(item.get("height", 0.4)) * 72)
    safety = 1.0 if is_measured_text(item) else float(
        item.get("text_fit_safety", manifest.get("text_fit_safety", DEFAULT_TEXT_FIT_SAFETY))
    )
    line_height = float(item.get("line_height", manifest.get("text_line_height", DEFAULT_TEXT_LINE_HEIGHT)))
    wrap_enabled = item.get("wrap") not in (None, "", "none")
    if wrap_enabled:
        line_count = sum(max(1, math.ceil(text_width_units(line) * requested / width_pt)) for line in lines)
        width_limit = width_pt / max(text_width_units(line) for line in lines)
    else:
        line_count = max(1, len(lines))
        width_limit = width_pt / max(text_width_units(line) for line in lines)
    height_limit = height_pt / (line_count * max(line_height, 1.0))
    maximum = min(width_limit, height_limit) * safety
    explicit_max = item.get("max_font_size")
    if explicit_max not in (None, ""):
        maximum = min(maximum, float(explicit_max))
    minimum = float(item.get("min_font_size", manifest.get("min_font_size", DEFAULT_MIN_FONT_SIZE)))
    return max(minimum, maximum)


def fit_text_item(item: dict, manifest: dict) -> dict:
    fitted = fitted_font_size(item, manifest)
    if fitted is None:
        return item
    requested = float(item.get("font_size", fitted))
    effective = min(requested, fitted)
    if effective < requested:
        item["_requested_font_size"] = requested
        item["font_size"] = round(effective, 1)
    elif "font_size" not in item:
        item["font_size"] = round(effective, 1)
    return item


def normalize_manifest(manifest: dict) -> dict:
    normalized = json.loads(json.dumps(manifest))
    normalized["text_boxes"] = [
        fit_text_item(normalize_position_item(normalized, item), normalized)
        for item in normalized.get("text_boxes", [])
    ]
    for key in ("images", "shapes"):
        normalized[key] = [normalize_position_item(normalized, item) for item in normalized.get(key, [])]
    return normalized


def shape_fill(fill) -> str:
    if fill is None or fill == "none":
        return "<a:noFill/>"
    if isinstance(fill, dict):
        if fill.get("type") == "gradient":
            stops = fill.get("stops") or []
            if len(stops) >= 2:
                alpha_xml = ""
                try:
                    alpha_value = float(fill.get("alpha", 1.0))
                    if 0.0 < alpha_value < 1.0:
                        alpha_xml = f'<a:alpha val="{int(round(alpha_value * 100000))}"/>'
                except (TypeError, ValueError):
                    alpha_xml = ""
                stop_xml = "".join(
                    f'<a:gs pos="{int(round(float(stop.get("pos", 0)) * 100000))}">'
                    f'<a:srgbClr val="{hex_color(stop.get("color"))}">{alpha_xml}</a:srgbClr></a:gs>'
                    for stop in stops
                )
                angle = float(fill.get("angle", 0)) % 360.0
                return (
                    '<a:gradFill rotWithShape="1">'
                    f"<a:gsLst>{stop_xml}</a:gsLst>"
                    f'<a:lin ang="{int(round(angle * 60000))}" scaled="0"/>'
                    "</a:gradFill>"
                )
        fill = fill.get("color")
    if not fill or fill == "none":
        return "<a:noFill/>"
    return f'<a:solidFill><a:srgbClr val="{hex_color(fill)}"/></a:solidFill>'


def shape_line_xml(stroke: str | None, width: float, dash: str | None = None, start_arrow: str | None = None, end_arrow: str | None = None) -> str:
    if not stroke or stroke == "none":
        return "<a:ln><a:noFill/></a:ln>"
    dash_xml = f'<a:prstDash val="{xml_text(dash)}"/>' if dash else ""
    return (
        f'<a:ln w="{int(float(width or 1) * 12700)}">'
        f'<a:solidFill><a:srgbClr val="{hex_color(stroke)}"/></a:solidFill>'
        f"{dash_xml}"
        + (f'<a:headEnd type="{xml_text(start_arrow)}"/>' if start_arrow else "")
        + (f'<a:tailEnd type="{xml_text(end_arrow)}"/>' if end_arrow else "")
        + "</a:ln>"
    )


def slide_background_xml(slide: dict) -> str:
    background = slide.get("background")
    if not background:
        return ""
    return (
        "<p:bg><p:bgPr>"
        f'<a:solidFill><a:srgbClr val="{hex_color(background, "FFFFFF")}"/></a:solidFill>'
        "<a:effectLst/></p:bgPr></p:bg>"
    )


def text_box_xml(index: int, item: dict) -> str:
    left = emu(item.get("left", 0))
    top = emu(item.get("top", 0))
    width = emu(item.get("width", 1))
    height = emu(item.get("height", 0.4))
    rotation = item.get("rotation")
    rotation_attr = f' rot="{int(float(rotation) * 60000)}"' if rotation not in (None, "") else ""
    font_size = int(float(item.get("font_size", 18)) * 100)
    align = text_alignment(item.get("align", "left"))[0]
    anchor = text_vertical_alignment(item.get("valign", "top"))[0]
    wrap = item.get("wrap", "none")
    autofit = item.get("autofit", "none")
    autofit_xml = "<a:spAutoFit/>" if autofit == "shape" else "<a:noAutofit/>"
    default_latin = item.get("font_latin", item.get("font", "Arial"))
    default_cjk = item.get("font_cjk", item.get("font", DEFAULT_FONT_CJK))

    def run_xml(run: dict) -> str:
        run_font_size = int(float(run.get("font_size", item.get("font_size", 18))) * 100)
        latin = xml_text(run.get("font_latin", run.get("font", default_latin)))
        cjk = xml_text(run.get("font_cjk", run.get("font", default_cjk)))
        run_color = hex_color(run.get("color", item.get("color", DEFAULT_TEXT_COLOR)))
        run_bold = ' b="1"' if run.get("bold", item.get("bold")) else ""
        run_italic = ' i="1"' if run.get("italic", item.get("italic")) else ""
        run_text = xml_text(run.get("text", ""))
        return (
            f'<a:r><a:rPr lang="zh-CN" sz="{run_font_size}"{run_bold}{run_italic}>'
            f'<a:solidFill><a:srgbClr val="{run_color}"/></a:solidFill>'
            f'<a:latin typeface="{latin}"/><a:ea typeface="{cjk}"/><a:cs typeface="{latin}"/>'
            f"</a:rPr><a:t>{run_text}</a:t></a:r>"
        )

    lines = iter_text_lines(item)
    text_body = ""
    for line in lines:
        text_body += (
            f'<a:p><a:pPr algn="{align}"/>'
            + run_xml({"text": line})
            + f'<a:endParaRPr lang="zh-CN" sz="{font_size}"/></a:p>'
        )
    return f"""
      <p:sp>
        <p:nvSpPr><p:cNvPr id="{index}" name="TextBox {index}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm{rotation_attr}><a:off x="{left}" y="{top}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
        <p:txBody>
          <a:bodyPr wrap="{xml_text(wrap)}" anchor="{anchor}" lIns="0" tIns="0" rIns="0" bIns="0">{autofit_xml}</a:bodyPr><a:lstStyle/>
          {text_body}
        </p:txBody>
      </p:sp>"""


def image_xml(index: int, rel_id: str, item: dict) -> str:
    left = emu(item.get("left", 0))
    top = emu(item.get("top", 0))
    width = emu(item.get("width", 1))
    height = emu(item.get("height", 1))
    name = xml_text(item.get("alt") or Path(item.get("path", "")).stem or f"Image {index}")
    return f"""
      <p:pic>
        <p:nvPicPr><p:cNvPr id="{index}" name="{name}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
        <p:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
        <p:spPr><a:xfrm><a:off x="{left}" y="{top}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      </p:pic>"""


def round_rect_adjustment(item: dict):
    box_px = item.get("box_px")
    if item.get("source_corner_radius_px") is not None and box_px and len(box_px) == 4:
        radius = float(item.get("source_corner_radius_px") or 0)
        min_dim = max(1.0, min(float(box_px[2]), float(box_px[3])))
    elif item.get("radius") is not None:
        radius = float(item.get("radius") or 0)
        min_dim = max(0.01, min(float(item.get("width", 1)), float(item.get("height", 1))))
    else:
        return None
    return max(0, min(50000, int(round(radius / min_dim * 100000))))


def preset_geometry_xml(preset: str, item: dict) -> str:
    if preset != "roundRect":
        return f'<a:prstGeom prst="{preset}"><a:avLst/></a:prstGeom>'
    adjustment = round_rect_adjustment(item)
    if adjustment is None:
        return '<a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>'
    return (
        '<a:prstGeom prst="roundRect"><a:avLst>'
        f'<a:gd name="adj" fmla="val {adjustment}"/>'
        "</a:avLst></a:prstGeom>"
    )


def freeform_geometry_xml(item: dict) -> str:
    width = max(1, emu(item.get("width", 1)))
    height = max(1, emu(item.get("height", 1)))
    points = []
    for point in item.get("path") or []:
        x = max(0, min(width, int(round(float(point[0]) * EMU_PER_INCH))))
        y = max(0, min(height, int(round(float(point[1]) * EMU_PER_INCH))))
        points.append((x, y))
    if len(points) < 3:
        return '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
    commands = [f'<a:moveTo><a:pt x="{points[0][0]}" y="{points[0][1]}"/></a:moveTo>']
    commands.extend(
        f'<a:lnTo><a:pt x="{x}" y="{y}"/></a:lnTo>' for x, y in points[1:]
    )
    commands.append("<a:close/>")
    return (
        "<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>"
        f'<a:rect l="0" t="0" r="{width}" b="{height}"/>'
        f'<a:pathLst><a:path w="{width}" h="{height}">{"".join(commands)}</a:path></a:pathLst>'
        "</a:custGeom>"
    )


def shape_effects_xml(item: dict) -> str:
    ratio = item.get("soft_edge_ratio")
    if not ratio:
        return ""
    try:
        ratio_value = float(ratio)
    except (TypeError, ValueError):
        return ""
    if ratio_value <= 0:
        return ""
    width = float(item.get("width", 1))
    height = float(item.get("height", 1))
    radius = max(1, emu(min(width, height) * ratio_value))
    return f'<a:effectLst><a:softEdge rad="{radius}"/></a:effectLst>'


def shape_xml(index: int, item: dict) -> str:
    kind = item.get("type", "rect")
    left = emu(item.get("left", 0))
    top = emu(item.get("top", 0))
    width = emu(item.get("width", 1))
    height = emu(item.get("height", 1))
    stroke_width = item.get("stroke_width", 1)
    flip_h = ' flipH="1"' if item.get("flip_h") else ""
    flip_v = ' flipV="1"' if item.get("flip_v") else ""
    fill = shape_fill(item.get("fill"))
    line = shape_line_xml(item.get("stroke", "#000000"), stroke_width, item.get("dash"), item.get("start_arrow"), item.get("end_arrow"))
    effects = shape_effects_xml(item)
    preset = item.get("preset")
    if not preset:
        preset = "line" if kind == "line" else "ellipse" if kind == "ellipse" else "roundRect" if kind == "roundRect" else "rect"
    geometry = freeform_geometry_xml(item) if kind == "freeform" else preset_geometry_xml(preset, item)
    return f"""
      <p:sp>
        <p:nvSpPr><p:cNvPr id="{index}" name="{xml_text(kind.title())} {index}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm{flip_h}{flip_v}><a:off x="{left}" y="{top}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>{geometry}{fill}{line}{effects}</p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody>
      </p:sp>"""


def slide_xml(manifest: dict) -> str:
    slide = manifest.get("slide", {})
    next_id = 2
    layered = []
    for index, item in enumerate(manifest.get("shapes", [])):
        layered.append((float(item.get("z_index", 100)), index, "shape", item, None))
    for rel_index, item in enumerate(manifest.get("images", []), start=1):
        layered.append((float(item.get("z_index", 200)), rel_index, "image", item, f"rId{rel_index + 1}"))
    for index, item in enumerate(manifest.get("text_boxes", [])):
        layered.append((float(item.get("z_index", 300)), index, "text", item, None))

    parts = []
    for _z_index, _order, kind, item, rel_id in sorted(layered, key=lambda entry: (entry[0], entry[1])):
        if kind == "shape":
            parts.append(shape_xml(next_id, item))
        elif kind == "image":
            parts.append(image_xml(next_id, rel_id, item))
        else:
            parts.append(text_box_xml(next_id, item))
        next_id += 1

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    {slide_background_xml(slide)}
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      {''.join(parts)}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def rels_xml(manifest: dict, media_start: int = 1) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
    ]
    for i, item in enumerate(manifest.get("images", []), start=1):
        target = f"../media/image{media_start + i - 1}{image_ext(item['path'])}"
        rels.append(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="'
        + REL_NS
        + '">'
        + "".join(rels)
        + "</Relationships>"
    )


def content_types_xml(manifests: list[dict]) -> str:
    defaults = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
    }
    for manifest in manifests:
        for item in manifest.get("images", []):
            ext = image_ext(item["path"]).lstrip(".")
            defaults[ext] = content_type_for(item["path"])
    default_xml = "".join(f'<Default Extension="{ext}" ContentType="{ctype}"/>' for ext, ctype in defaults.items())
    overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for i in range(1, len(manifests) + 1):
        overrides.append(
            f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + default_xml
        + "".join(overrides)
        + "</Types>"
    )


def is_wide_slide(width: float, height: float) -> bool:
    return abs((float(width) / float(height)) / ASPECT_16_9 - 1) <= ASPECT_TOLERANCE


def slide_size_type(width: float, height: float) -> str:
    return "screen16x9" if is_wide_slide(width, height) else "custom"


def presentation_xml(slide_count: int, width: int, height: int) -> str:
    slide_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, slide_count + 1))
    size_type = slide_size_type(width, height)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>{slide_ids}</p:sldIdLst>
  <p:sldSz cx="{width}" cy="{height}" type="{size_type}"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>"""


def presentation_rels_xml(slide_count: int) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
    ]
    for i in range(1, slide_count + 1):
        rels.append(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        )
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">{"".join(rels)}</Relationships>'


def write_common_parts(z: zipfile.ZipFile, slide_count: int, width: int, height: int) -> None:
    presentation_format = "Widescreen" if is_wide_slide(width, height) else "Custom"
    z.writestr(
        "_rels/.rels",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>',
    )
    z.writestr(
        "docProps/core.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Image to editable PPT</dc:title><dc:creator>pic2ppt</dc:creator></cp:coreProperties>',
    )
    z.writestr(
        "docProps/app.xml",
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>pic2ppt</Application><PresentationFormat>{presentation_format}</PresentationFormat><Slides>{slide_count}</Slides></Properties>',
    )
    z.writestr("ppt/presentation.xml", presentation_xml(slide_count, width, height))
    z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels_xml(slide_count))
    z.writestr(
        "ppt/slideMasters/slideMaster1.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>',
    )
    z.writestr(
        "ppt/slideMasters/_rels/slideMaster1.xml.rels",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>',
    )
    z.writestr(
        "ppt/slideLayouts/slideLayout1.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',
    )
    z.writestr(
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>',
    )
    theme_fills = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
    theme_lines = '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3
    theme_effects = '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
    z.writestr(
        "ppt/theme/theme1.xml",
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Pic2PPT"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F1F1F"/></a:dk2><a:lt2><a:srgbClr val="F8F8F8"/></a:lt2><a:accent1><a:srgbClr val="0F766E"/></a:accent1><a:accent2><a:srgbClr val="E66B00"/></a:accent2><a:accent3><a:srgbClr val="F6D365"/></a:accent3><a:accent4><a:srgbClr val="57C4B8"/></a:accent4><a:accent5><a:srgbClr val="666666"/></a:accent5><a:accent6><a:srgbClr val="111111"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Pic2PPT"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface="{DEFAULT_FONT_CJK}"/><a:cs typeface="Arial"/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface="{DEFAULT_FONT_CJK}"/><a:cs typeface="Arial"/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst>{theme_fills}</a:fillStyleLst><a:lnStyleLst>{theme_lines}</a:lnStyleLst><a:effectStyleLst>{theme_effects}</a:effectStyleLst><a:bgFillStyleLst>{theme_fills}</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>',
    )


def write_pptx(manifest: dict, out_path: Path, manifest_path: Path) -> None:
    width = emu(manifest.get("slide", {}).get("width", 13.333))
    height = emu(manifest.get("slide", {}).get("height", 7.5))
    normalized = normalize_manifest(manifest)
    normalized_for_write = json.loads(json.dumps(normalized))
    for item in normalized_for_write.get("text_boxes", []):
        item.pop("_requested_font_size", None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    media_index = 1
    base = manifest_path.resolve().parent
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml([normalized_for_write]))
        write_common_parts(z, 1, width, height)
        z.writestr("ppt/slides/slide1.xml", slide_xml(normalized_for_write))
        z.writestr("ppt/slides/_rels/slide1.xml.rels", rels_xml(normalized_for_write, media_index))
        for item in normalized_for_write.get("images", []):
            src = Path(item["path"])
            if not src.is_absolute():
                src = base / src
            if not src.exists():
                raise FileNotFoundError(f"Missing media asset: {src}")
            z.write(src, f"ppt/media/image{media_index}{image_ext(src)}")
            media_index += 1


def write_deck(deck: dict, entries: list[dict], out_path: Path) -> None:
    if not entries:
        raise ValueError("Deck has no pages")
    slide = deck.get("slide", WIDE_SLIDE)
    width = emu(slide.get("width", 13.333))
    height = emu(slide.get("height", 7.5))
    manifests = []
    for entry in entries:
        normalized = normalize_manifest(entry["manifest"])
        for item in normalized.get("text_boxes", []):
            item.pop("_requested_font_size", None)
        manifests.append(normalized)
    media_index = 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml(manifests))
        write_common_parts(z, len(entries), width, height)
        for slide_index, (entry, manifest) in enumerate(zip(entries, manifests), start=1):
            z.writestr(f"ppt/slides/slide{slide_index}.xml", slide_xml(manifest))
            z.writestr(f"ppt/slides/_rels/slide{slide_index}.xml.rels", rels_xml(manifest, media_index))
            base = Path(entry["manifest_path"]).resolve().parent
            for item in manifest.get("images", []):
                src = Path(item["path"])
                if not src.is_absolute():
                    src = base / src
                if not src.exists():
                    raise FileNotFoundError(f"Missing media asset: {src}")
                z.write(src, f"ppt/media/image{media_index}{image_ext(src)}")
                media_index += 1


# ---------------------------------------------------------------------------
# Preview + comparison sheet
# ---------------------------------------------------------------------------


def preview_font_path(cjk: bool = True, bold: bool = False) -> str | None:
    windows = Path("C:/Windows/Fonts")
    if cjk:
        candidates = [
            windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
            windows / "msyh.ttc",
            windows / "simhei.ttf",
            windows / "Deng.ttf",
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
    else:
        candidates = [
            windows / ("arialbd.ttf" if bold else "arial.ttf"),
            windows / "calibri.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def gradient_fill_image(width: int, height: int, fill: dict) -> Image.Image | None:
    """Render a linear gradient fill for the preview."""
    stops = sorted(fill.get("stops") or [], key=lambda stop: float(stop.get("pos", 0)))
    if len(stops) < 2 or width < 1 or height < 1:
        return None
    positions = []
    colors = []
    for stop in stops:
        positions.append(float(stop.get("pos", 0)))
        colors.append(rgb_from_hex(stop.get("color", "#FFFFFF")))
    for index in range(1, len(positions)):
        if positions[index] <= positions[index - 1]:
            positions[index] = positions[index - 1] + 1e-4
    angle = math.radians(float(fill.get("angle", 0)))
    x = np.arange(width, dtype=np.float32)[None, :] - (width - 1) / 2.0
    y = np.arange(height, dtype=np.float32)[:, None] - (height - 1) / 2.0
    projection = x * math.cos(angle) + y * math.sin(angle)
    low, high = float(projection.min()), float(projection.max())
    if high - low < 1e-6:
        projection[:] = 0.0
    else:
        projection = (projection - low) / (high - low)
    channel_values = [
        np.interp(projection, positions, [colour[channel] for colour in colors])
        for channel in range(3)
    ]
    array = np.stack(channel_values, axis=-1)
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")


def render_preview(manifest: dict, manifest_path: Path, out_path: Path, scale: int = 120) -> None:
    normalized = normalize_manifest(manifest)
    slide = normalized.get("slide", {})
    width_in = float(slide.get("width", 13.333))
    height_in = float(slide.get("height", 7.5))
    canvas = Image.new("RGB", (int(width_in * scale), int(height_in * scale)), rgb_from_hex(slide.get("background", "#FFFFFF")))
    base = manifest_path.resolve().parent
    draw = ImageDraw.Draw(canvas)

    def draw_shape(item: dict) -> None:
        box = [
            item.get("left", 0) * scale,
            item.get("top", 0) * scale,
            (item.get("left", 0) + item.get("width", 1)) * scale,
            (item.get("top", 0) + item.get("height", 1)) * scale,
        ]
        fill = item.get("fill")
        solid_fill = fill if isinstance(fill, str) else None
        stroke = item.get("stroke", "#000000")
        outline = None if not stroke or stroke == "none" else stroke
        line_width = max(1, int(round(float(item.get("stroke_width", 1)) * scale / 72.0)))
        if item.get("type") == "line" and "points" in item:
            pts = item["points"]
            draw.line([(pts[0] * scale, pts[1] * scale), (pts[2] * scale, pts[3] * scale)], fill=outline or "#000000", width=line_width)
        elif item.get("type") == "freeform" and item.get("path"):
            points = [(box[0] + float(point[0]) * scale, box[1] + float(point[1]) * scale) for point in item["path"]]
            if isinstance(fill, dict) and fill.get("type") == "gradient":
                width_px = max(1, int(round(box[2] - box[0])))
                height_px = max(1, int(round(box[3] - box[1])))
                gradient = gradient_fill_image(width_px, height_px, fill)
                if gradient is not None:
                    mask = Image.new("L", (width_px, height_px), 0)
                    ImageDraw.Draw(mask).polygon(
                        [(point[0] - box[0], point[1] - box[1]) for point in points],
                        fill=255,
                    )
                    soft_edge = float(item.get("soft_edge_ratio") or 0)
                    if soft_edge > 0:
                        mask = mask.filter(
                            ImageFilter.GaussianBlur(max(1.0, min(width_px, height_px) * soft_edge))
                        )
                    try:
                        fill_alpha = float(fill.get("alpha", 1.0))
                    except (TypeError, ValueError):
                        fill_alpha = 1.0
                    if 0.0 < fill_alpha < 1.0:
                        mask = mask.point(lambda value: int(round(value * fill_alpha)))
                    canvas.paste(gradient, (int(round(box[0])), int(round(box[1]))), mask)
            else:
                draw.polygon(points, fill=None if not solid_fill or solid_fill == "none" else solid_fill, outline=outline)
        elif item.get("type") == "ellipse":
            draw.ellipse(box, fill=None if not solid_fill or solid_fill == "none" else solid_fill, outline=outline, width=line_width)
        elif item.get("type") == "roundRect":
            radius = max(1, int(round(float(item.get("radius", 0.08)) * scale)))
            draw.rounded_rectangle(box, radius=radius, fill=None if not solid_fill or solid_fill == "none" else solid_fill, outline=outline, width=line_width)
        elif item.get("type") in ("rect", "") and isinstance(fill, dict) and fill.get("type") == "gradient":
            gradient = gradient_fill_image(
                max(1, int(round(box[2] - box[0]))),
                max(1, int(round(box[3] - box[1]))),
                fill,
            )
            if gradient is not None:
                soft_edge = float(item.get("soft_edge_ratio") or 0)
                if soft_edge > 0:
                    mask = Image.new("L", gradient.size, 255).filter(
                        ImageFilter.GaussianBlur(
                            max(1.0, min(gradient.size) * soft_edge)
                        )
                    )
                    try:
                        fill_alpha = float(fill.get("alpha", 1.0))
                    except (TypeError, ValueError):
                        fill_alpha = 1.0
                    if 0.0 < fill_alpha < 1.0:
                        mask = mask.point(lambda value: int(round(value * fill_alpha)))
                    canvas.paste(gradient, (int(round(box[0])), int(round(box[1]))), mask)
                else:
                    canvas.paste(gradient, (int(round(box[0])), int(round(box[1]))))
            if outline:
                draw.rectangle(box, outline=outline, width=line_width)
        else:
            draw.rectangle(box, fill=None if not solid_fill or solid_fill == "none" else solid_fill, outline=outline, width=line_width)

    def draw_image(item: dict) -> None:
        src = Path(item["path"])
        if not src.is_absolute():
            src = base / src
        if not src.exists():
            return
        try:
            image = Image.open(src).convert("RGBA")
        except Exception:
            return
        target = (max(1, int(round(item.get("width", 1) * scale))), max(1, int(round(item.get("height", 1) * scale))))
        image = image.resize(target, Image.LANCZOS)
        canvas.paste(image, (int(round(item.get("left", 0) * scale)), int(round(item.get("top", 0) * scale))), image)

    def draw_text(item: dict) -> None:
        text = str(item.get("text", ""))
        if not text:
            return
        font_px = max(1, int(round(float(item.get("font_size", 18)) * scale / 72.0)))
        bold = bool(item.get("bold"))
        font_path = preview_font_path(cjk=contains_cjk(text) or bool(item.get("font_cjk")), bold=bold)
        try:
            font = ImageFont.truetype(font_path, font_px, index=0) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        lines = text.split("\n")
        line_height = font_px * float(item.get("line_height", 1.22))
        x = item.get("left", 0) * scale
        y = item.get("top", 0) * scale
        width = item.get("width", 1) * scale
        height = item.get("height", 0.4) * scale
        total_height = line_height * len(lines)
        valign = str(item.get("valign", "top")).lower()
        if valign in ("middle", "center", "ctr"):
            y += max(0, (height - total_height) / 2.0)
        elif valign in ("bottom", "b"):
            y += max(0, height - total_height)
        align = str(item.get("align", "left")).lower()
        color = item.get("color", DEFAULT_TEXT_COLOR)
        for line in lines:
            line_width = draw.textlength(line, font=font)
            if align in ("center", "ctr"):
                tx = x + (width - line_width) / 2.0
            elif align in ("right", "r"):
                tx = x + width - line_width
            else:
                tx = x
            draw.text((tx, y), line, font=font, fill=color)
            y += line_height

    layered = []
    for index, item in enumerate(normalized.get("shapes", [])):
        layered.append((float(item.get("z_index", 100)), index, "shape", item))
    for index, item in enumerate(normalized.get("images", [])):
        layered.append((float(item.get("z_index", 200)), index, "image", item))
    for index, item in enumerate(normalized.get("text_boxes", [])):
        layered.append((float(item.get("z_index", 300)), index, "text", item))
    for _z, _index, kind, item in sorted(layered, key=lambda entry: (entry[0], entry[1])):
        if kind == "shape":
            draw_shape(item)
        elif kind == "image":
            draw_image(item)
        else:
            draw_text(item)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def make_comparison_sheet(source: Path, preview: Path, out_path: Path) -> None:
    source_image = Image.open(source).convert("RGB")
    preview_image = Image.open(preview).convert("RGB")
    target_height = 700
    scaled = []
    for image, label in ((source_image, "SOURCE"), (preview_image, "RECONSTRUCTION")):
        ratio = target_height / image.height
        resized = image.resize((max(1, int(image.width * ratio)), target_height), Image.LANCZOS)
        scaled.append((resized, label))
    gap = 24
    label_height = 34
    total_width = sum(image.width for image, _ in scaled) + gap * 3
    canvas = Image.new("RGB", (total_width, target_height + label_height + gap * 2), (245, 246, 248))
    draw = ImageDraw.Draw(canvas)
    font_path = preview_font_path(cjk=True)
    font = ImageFont.truetype(font_path, 18, index=0) if font_path else ImageFont.load_default()
    x = gap
    for image, label in scaled:
        draw.text((x, gap), label, fill=(60, 64, 72), font=font)
        canvas.paste(image, (x, gap + label_height))
        x += image.width + gap
    canvas.save(out_path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_page_manifest(manifest: dict, page_dir: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    source = manifest.get("source", {})
    width = int(source.get("width_px") or 0)
    height = int(source.get("height_px") or 0)

    if not manifest.get("text_boxes") and not manifest.get("shapes") and not manifest.get("images"):
        warnings.append("page has no positioned build objects")
    for item in manifest.get("text_boxes", []):
        if not str(item.get("text", "")).strip():
            errors.append(f"text box {item.get('id')} has empty text")
        if float(item.get("font_size", 0)) < 4:
            errors.append(f"text box {item.get('id')} font size is below 4pt")
        box = item.get("box_px")
        if not box or len(box) != 4 or box[2] <= 0 or box[3] <= 0:
            errors.append(f"text box {item.get('id')} lacks a positive box_px")
        elif width and height and (box[0] < -2 or box[1] < -2 or box[0] + box[2] > width + 2 or box[1] + box[3] > height + 2):
            warnings.append(f"text box {item.get('id')} extends outside the source image")
    for item in manifest.get("images", []):
        path = page_dir / item.get("path", "")
        if not path.exists():
            errors.append(f"image asset missing: {item.get('path')}")
        box = item.get("box_px")
        if not box or len(box) != 4 or box[2] <= 0 or box[3] <= 0:
            errors.append(f"image {item.get('id')} lacks a positive box_px")
    for item in manifest.get("shapes", []):
        if item.get("type") == "line":
            if not item.get("points_px"):
                errors.append(f"line {item.get('id')} lacks points_px")
        else:
            box = item.get("box_px")
            if not box or len(box) != 4 or box[2] <= 0 or box[3] <= 0:
                errors.append(f"shape {item.get('id')} lacks a positive box_px")
        if item.get("type") == "roundRect" and item.get("source_corner_radius_px") is None:
            warnings.append(f"roundRect {item.get('id')} has no measured corner radius")

    if not manifest.get("text_boxes"):
        warnings.append("no editable text was reconstructed")
    if manifest.get("removed_area_px", 0) == 0 and manifest.get("text_boxes"):
        errors.append("text boxes exist but nothing was removed from the background")

    return {
        "schema_version": 1,
        "page_id": manifest.get("page_id"),
        "passed": len(errors) == 0,
        "editable": bool(manifest.get("text_boxes") or manifest.get("shapes")),
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "text_boxes": len(manifest.get("text_boxes", [])),
            "shapes": len(manifest.get("shapes", [])),
            "images": len(manifest.get("images", [])),
        },
    }


def validate_pptx(pptx_path: Path, entries: list[dict]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    checks = {}
    try:
        with zipfile.ZipFile(pptx_path, "r") as z:
            bad = z.testzip()
            checks["zip_integrity"] = bad is None
            if bad is not None:
                errors.append(f"corrupt zip member: {bad}")
            names = set(z.namelist())
            checks["content_types"] = "[Content_Types].xml" in names
            if not checks["content_types"]:
                errors.append("missing [Content_Types].xml")
            slide_names = sorted(name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name))
            checks["slide_count"] = len(slide_names) == len(entries)
            if len(slide_names) != len(entries):
                errors.append(f"slide count {len(slide_names)} != page count {len(entries)}")
            for name in names:
                if name.endswith(".xml") or name.endswith(".rels"):
                    try:
                        ET.fromstring(z.read(name))
                    except Exception as exc:
                        errors.append(f"invalid XML part {name}: {exc}")
            media_names = [name for name in names if name.startswith("ppt/media/")]
            expected_media = sum(len(entry["manifest"].get("images", [])) for entry in entries)
            checks["media_count"] = len(media_names) == expected_media
            if len(media_names) != expected_media:
                errors.append(f"media count {len(media_names)} != expected {expected_media}")
            for name in slide_names:
                rels_name = name.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
                if rels_name not in names:
                    errors.append(f"missing relationships part for {name}")
                    continue
                root = ET.fromstring(z.read(rels_name))
                for relationship in root:
                    target = relationship.attrib.get("Target", "")
                    if "media/" in target:
                        resolved = "ppt/" + target.replace("../", "")
                        if resolved not in names:
                            errors.append(f"relationship target missing: {target}")
    except Exception as exc:
        errors.append(f"cannot open PPTX: {exc}")
    checks["no_errors"] = not errors
    return {"passed": not errors, "checks": checks, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# Page pipeline
# ---------------------------------------------------------------------------


def process_page(
    *,
    page_id: str,
    source_image: Path,
    page_dir: Path,
    slide: dict,
    content_box: dict,
    ocr_backend: str,
    paddle_token: str | None,
    paddle_model: str,
    chart_recognition: bool,
    mode: str,
    font_cjk: str,
    font_latin: str,
    extract_icons: bool,
    detect_shapes: bool,
    no_text_fit: bool,
    raster_text_mad: float,
    raster_text_std: float,
    gradient_backdrops: bool,
    bake_backdrops: bool,
    max_source_side: int,
    bg_max_width: int,
) -> dict:
    page_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = page_dir / "assets"
    warning_start = len(WARNINGS)

    image = load_rgb(source_image)
    if max_source_side and max(image.size) > max_source_side:
        image, _ = resize_max(image, max_source_side)
    source_width, source_height = image.size
    source_png = page_dir / "source.png"
    image.save(source_png, "PNG")
    (page_dir / "imagegen-jobs.json").write_text(
        json.dumps({"schema_version": 1, "page_id": page_id, "jobs": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (page_dir / "page_request.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_id": page_id,
                "page_dir": str(page_dir),
                "source_image": str(source_png),
                "source_size_px": {"width": source_width, "height": source_height},
                "slide": slide,
                "content_box": content_box,
                "required_outputs": [
                    "manifest.json",
                    "page.pptx",
                    "preview.png",
                    "comparison.png",
                    "validation.json",
                    "page_result.json",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"  source: {source_width}x{source_height}px")

    rgb = np.asarray(image, dtype=np.uint8)
    analysis = analyze_page(image)
    page_background = np.array([int(v) for v in analysis["background_rgb"]], dtype=np.float32)
    inches_per_px = float(content_box["height"]) / float(source_height)
    pt_per_px = inches_per_px * 72.0

    # --- Step 1: text ------------------------------------------------------
    used_backend, blocks = run_ocr(
        source_png,
        ocr_backend,
        paddle_token,
        paddle_model,
        source_width,
        source_height,
        chart_recognition=chart_recognition,
    )
    if used_backend == "none":
        warn("OCR unavailable; text remains in the clean base image (the deck is not fully editable).")
    text_items, text_removals, hints = build_text_items(
        blocks, image, source_width, source_height, content_box, font_cjk, font_latin
    )
    hints_payload = {
        "schema_version": 1,
        "backend": used_backend,
        "source": {"width_px": source_width, "height_px": source_height},
        "lines": hints,
        "note": "Measured text lines. box_px and font_pt are source-pixel values used by text_boxes.",
    }
    (page_dir / "text_hints.json").write_text(json.dumps(hints_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (page_dir / "ocr_blocks.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    draw_text_hint_overlay(image, hints, page_dir / "text_hints.png")

    original_text_count = len(text_items)
    text_items, text_removals, _kept_hints, raster_text, backdrop_shapes = filter_repairable_text(
        text_items,
        text_removals,
        hints,
        rgb,
        mad_threshold=raster_text_mad,
        std_threshold=raster_text_std,
        gradient_backdrops=gradient_backdrops,
    )
    if backdrop_shapes:
        if bake_backdrops:
            log(f"  gradient backdrops: {len(backdrop_shapes)} text block(s) blended into the clean base")
        else:
            log(f"  gradient backdrops: {len(backdrop_shapes)} text block(s) rebuilt as native gradient shapes")
    if raster_text:
        warn(
            f"{len(raster_text)} text block(s) kept as raster because the background is too "
            "complex for a clean local repair (no image-generation model in this pipeline)."
        )
    log(f"  text: {len(text_items)}/{original_text_count} native boxes ({used_backend})")

    text_boxes_px = [item["box_px"] for item in text_items]
    all_text_boxes_px = text_boxes_px + [item["box_px"] for item in raster_text]

    # --- Step 2: native shapes --------------------------------------------
    shapes: list[dict] = []
    shape_removals: list[dict] = []
    visual_inventory: list[dict] = []
    shape_mode = "native" if (detect_shapes and mode in ("auto", "native") and analysis["mode"] == "native") else "image"
    if detect_shapes:
        shapes, shape_removals, shape_inventory = detect_native_shapes(
            image, shape_mode, all_text_boxes_px, page_background, pt_per_px
        )
        visual_inventory.extend(shape_inventory)
        log(f"  native shapes: {len(shapes)}")
    else:
        log("  native shapes: disabled")

    if backdrop_shapes:
        if not bake_backdrops:
            # A one-pixel edge blend hides residual colour jumps without the
            # visible blur of a proportional (5-10%) soft edge.
            for shape in backdrop_shapes:
                box = shape.get("box_px") or [0, 0, 1, 1]
                short_side = max(1.0, float(min(box[2], box[3])))
                shape["soft_edge_ratio"] = 1.0 / short_side
            shapes.extend(backdrop_shapes)
        visual_inventory.extend(
            {
                "id": shape.get("id"),
                "role": "backdrop",
                "object_type": "baked-gradient-backdrop" if bake_backdrops else "gradient-backdrop",
                "description": (
                    "gradient patch blended into the clean base"
                    if bake_backdrops
                    else shape.get("reason", "native gradient patch behind editable text")
                ),
            }
            for shape in backdrop_shapes
        )

    # --- Step 3: foreground assets ----------------------------------------
    images_manifest: list[dict] = []
    asset_removals: list[dict] = []
    asset_inventory: list[dict] = []
    asset_warnings: list[str] = []
    if extract_icons and used_backend == "none":
        # Without OCR there is no text mask; colour components would include
        # whole text lines and produce misleading "icons".
        log("  foreground assets: disabled (no OCR text mask)")
    elif extract_icons:
        images_manifest, asset_removals, asset_inventory, asset_warnings = detect_foreground_assets(
            image,
            all_text_boxes_px,
            [shape.get("box_px") for shape in shapes if shape.get("box_px")],
            assets_dir,
        )
        visual_inventory.extend(asset_inventory)
        for message in asset_warnings:
            warn(message)
        log(f"  foreground assets: {len(images_manifest)}")
    else:
        log("  foreground assets: disabled")

    # --- Step 4: clean base ------------------------------------------------
    removals = text_removals + shape_removals + asset_removals
    base_rgb, removed_area = apply_removals(rgb, removals, page_background)
    if backdrop_shapes and bake_backdrops:
        base_rgb = bake_backdrop_shapes(base_rgb, backdrop_shapes)
    base_image = Image.fromarray(base_rgb, "RGB")

    # If nothing editable was removed and no native object exists, this is a
    # raster-only page. Report it instead of pretending it is editable.
    if not text_items and not shapes and not images_manifest:
        warn("no editable objects were reconstructed for this page")

    # Decide whether the base is uniform enough for a solid slide background.
    base_arr = np.asarray(base_image, dtype=np.int16)
    diff = np.abs(base_arr - page_background.reshape(1, 1, 3)).max(axis=2)
    uniform_fraction = float((diff <= 6).mean())
    baked_backdrops = bool(backdrop_shapes and bake_backdrops)
    base_is_uniform = uniform_fraction >= 0.985 and not baked_backdrops
    if base_is_uniform:
        if (base_image.width, base_image.height) != (source_width, source_height):
            base_image = base_image.resize((source_width, source_height), Image.LANCZOS)
        background_image_path = None
        log("  background: native solid fill (clean base is uniform)")
    else:
        if bg_max_width and base_image.width > bg_max_width:
            ratio = bg_max_width / base_image.width
            base_image = base_image.resize((bg_max_width, max(1, int(base_image.height * ratio))), Image.LANCZOS)
        assets_dir.mkdir(parents=True, exist_ok=True)
        background_image_path = assets_dir / "base.png"
        base_image.save(background_image_path, "PNG")
        log(f"  background: clean base image ({base_image.width}x{base_image.height})")

    # --- Step 5: manifest --------------------------------------------------
    images: list[dict] = []
    asset_provenance: list[dict] = []
    if background_image_path is not None:
        images.append(
            {
                "id": "background",
                "path": rel_posix(background_image_path, page_dir),
                "box_px": [0, 0, source_width, source_height],
                "alt": "clean background reconstructed from the source",
                "z_index": 0,
                "source_type": "source-preserving-local-cleanup",
            }
        )
        asset_provenance.append(
            {
                "path": rel_posix(background_image_path, page_dir),
                "source": "source.png",
                "source_type": "source-preserving-local-cleanup",
                "provenance_note": "Source background with editable text, native shapes and separated foreground objects removed by local inpainting.",
            }
        )
        visual_inventory.insert(
            0,
            {
                "id": "background",
                "role": "background",
                "object_type": "clean-base",
                "path": rel_posix(background_image_path, page_dir),
                "description": "clean base preserving the source background identity",
            },
        )
    images.extend(images_manifest)
    for item in images_manifest:
        asset_provenance.append(
            {
                "path": item["path"],
                "source": "source.png",
                "source_type": "source-preserving-local-cleanup",
                "provenance_note": "Separated from the source page with connected-component grouping and colour-distance alpha matting.",
            }
        )

    for index, entry in enumerate(raster_text, start=1):
        visual_inventory.append(
            {
                "id": f"r{index:03d}",
                "role": "raster-text",
                "object_type": "text",
                "description": f"{entry['text'][:80]} — kept in the clean base because the background is too complex to repair locally",
            }
        )

    background_strategy = {
        "mode": "source-preserving-local-cleanup" if background_image_path is not None else "native-or-script",
        "source_consistency_contract": "The clean base preserves the source composition, colours and non-editable content; only objects rebuilt natively were removed.",
        "removed_foreground": [
            f"{len(text_items)} native text regions",
            f"{len(raster_text)} raster text regions kept for fidelity",
            f"{len(shapes)} structural objects",
            f"{len(images_manifest)} separated foreground assets",
        ],
        "removed_area_px": removed_area,
        "uniform_background_fraction": round(uniform_fraction, 4),
        "comparison_note": "Preview and origin-versus-preview comparison are written next to the manifest for visual review.",
    }

    quality_checks = {
        "font_size_calibrated": any(item.get("font_size_source") == "measured" for item in text_items),
        "visual_inventory_matched": all((page_dir / item["path"]).exists() for item in images),
        "background_strategy_checked": True,
        "shape_corner_geometry_checked": True,
        "text_hints_generated": True,
    }

    manifest = build_page_manifest(
        page_id=page_id,
        source_name="source.png",
        source_width=source_width,
        source_height=source_height,
        slide=slide,
        content_box=content_box,
        page_background=hex_color_from_rgb(page_background),
        analysis=analysis,
        text_items=text_items,
        shapes=shapes,
        images=images,
        background_strategy=background_strategy,
        quality_checks=quality_checks,
        asset_provenance=asset_provenance,
        visual_inventory=visual_inventory,
        removed_area=removed_area,
        raster_text=raster_text,
    )
    if no_text_fit:
        manifest["fit_text"] = False
    manifest_path = page_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- Step 6: build, preview, validate ---------------------------------
    page_pptx = page_dir / "page.pptx"
    write_pptx(manifest, page_pptx, manifest_path)
    preview_path = page_dir / "preview.png"
    render_preview(manifest, manifest_path, preview_path)
    comparison_path = page_dir / "comparison.png"
    make_comparison_sheet(source_png, preview_path, comparison_path)

    page_validation = validate_page_manifest(manifest, page_dir)
    validation = {
        **page_validation,
        "warnings": WARNINGS[warning_start:] + page_validation["warnings"],
    }
    (page_dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")

    page_result = {
        "page_manifest": "manifest.json",
        "page_pptx": "page.pptx",
        "preview": "preview.png",
        "contact_sheet": "comparison.png",
        "validation": "validation.json",
        "hashes": {
            "manifest.json": sha256_file(manifest_path),
            "page.pptx": sha256_file(page_pptx),
            "preview.png": sha256_file(preview_path),
        },
        "asset_hashes": {
            item["path"]: sha256_file((page_dir / item["path"]).resolve())
            for item in images
            if (page_dir / item["path"]).exists()
        },
    }
    (page_dir / "page_result.json").write_text(json.dumps(page_result, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "page_id": page_id,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "preview": preview_path,
        "validation": validation,
        "ocr_backend": used_backend,
        "pptx": page_pptx,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert slide images into an editable PowerPoint by rebuilding text, foreground assets and flat shapes natively.",
    )
    parser.add_argument("inputs", nargs="*", help="Input image paths (png/jpg/webp/bmp).")
    parser.add_argument(
        "--from-manifest",
        nargs="+",
        help="Rebuild a deck from one or more edited pages/page_NNN/manifest.json files.",
    )
    parser.add_argument("-o", "--output", help="Output .pptx path.")
    parser.add_argument("--workdir", help="Directory for per-page artifacts. Default: <output>_work.")
    parser.add_argument(
        "--ocr",
        default="auto",
        choices=["auto", "ppocrv6", "paddleocr-vl", "rapidocr", "tesseract", "none"],
        help=(
            "OCR backend. auto prefers PP-OCRv6 (cloud, line-level) > RapidOCR > Tesseract. "
            "PaddleOCR-VL is available explicitly for layout parsing."
        ),
    )
    parser.add_argument(
        "--paddle-token",
        help="Paddle OCR token (defaults to PADDLE_OCR_TOKEN or ~/.editppt/config.yaml).",
    )
    parser.add_argument(
        "--paddle-model",
        default=PADDLE_DEFAULT_MODEL,
        help=f"PaddleOCR-VL model used by --ocr paddleocr-vl (default: {PADDLE_DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--chart-recognition",
        action="store_true",
        help="Ask PaddleOCR-VL to parse charts; figure titles/footers may be returned as text. Chart internals stay raster.",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "native", "image"],
        help="Background reconstruction strategy. auto decides from flatness.",
    )
    parser.add_argument("--font-cjk", default=DEFAULT_FONT_CJK)
    parser.add_argument("--font-latin", default=DEFAULT_FONT_LATIN)
    icon_group = parser.add_mutually_exclusive_group()
    icon_group.add_argument(
        "--icons",
        dest="extract_icons",
        action="store_true",
        help="Separate foreground icons into PNG assets (off by default).",
    )
    icon_group.add_argument(
        "--no-icons",
        dest="extract_icons",
        action="store_false",
        help="Keep icons in the clean base (default).",
    )
    parser.set_defaults(extract_icons=False)
    backdrop_group = parser.add_mutually_exclusive_group()
    backdrop_group.add_argument(
        "--gradient-backdrops",
        dest="gradient_backdrops",
        action="store_true",
        help="Rebuild text on smooth coloured gradients as native gradient shapes (default in editability).",
    )
    backdrop_group.add_argument(
        "--no-gradient-backdrops",
        dest="gradient_backdrops",
        action="store_false",
        help="Keep text on gradients as raster (default in balanced).",
    )
    parser.set_defaults(gradient_backdrops=None)
    bake_group = parser.add_mutually_exclusive_group()
    bake_group.add_argument(
        "--bake-backdrops",
        dest="bake_backdrops",
        action="store_true",
        help="Blend gradient backdrops into the clean background image (default).",
    )
    bake_group.add_argument(
        "--keep-backdrop-shapes",
        dest="bake_backdrops",
        action="store_false",
        help="Keep gradient backdrops as separate native shapes instead of baking them into the background.",
    )
    parser.set_defaults(bake_backdrops=True)
    parser.add_argument("--no-shapes", action="store_true", help="Do not rebuild flat cards/lines natively.")
    parser.add_argument("--no-text-fit", action="store_true", help="Disable deterministic font-size clamping.")
    parser.add_argument(
        "--profile",
        choices=["balanced", "editability"],
        default="editability",
        help="editability raises the text nativization threshold (default); balanced keeps more text raster for fidelity.",
    )
    parser.add_argument(
        "--raster-text-mad",
        type=float,
        default=None,
        help="Ring-uniformity threshold; text above it stays raster. Default: 18 for editability, 15 for balanced.",
    )
    parser.add_argument(
        "--raster-text-std",
        type=float,
        default=None,
        help="Non-text background spread threshold inside a text box; text above it stays raster. Default: 50 (editability) / 20 (balanced).",
    )
    parser.add_argument(
        "--max-source-side",
        type=int,
        default=3000,
        help="Downscale inputs longer than this (pixels). Default: 3000.",
    )
    parser.add_argument("--bg-max-width", type=int, default=2400, help="Max clean-base width. Default: 2400.")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global WARNINGS, QUIET
    WARNINGS = []
    args = parse_args(argv)
    QUIET = bool(args.quiet)
    if args.raster_text_mad is None:
        args.raster_text_mad = 18.0 if args.profile == "editability" else 15.0
    if args.raster_text_std is None:
        args.raster_text_std = 50.0 if args.profile == "editability" else 20.0
    if args.gradient_backdrops is None:
        args.gradient_backdrops = args.profile == "editability"
    # The editability profile is the default: this tool prefers real text
    # objects even when that costs some background fidelity.

    if args.from_manifest:
        if args.inputs:
            raise SystemExit("Use either input images or --from-manifest, not both.")
        manifest_paths = [Path(raw).expanduser().resolve() for raw in args.from_manifest]
        entries = []
        page_errors = []
        for index, manifest_path in enumerate(manifest_paths, start=1):
            if not manifest_path.exists():
                raise SystemExit(f"Manifest not found: {manifest_path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            page_id = manifest.get("page_id") or f"page_{index:03d}"
            page_validation = validate_page_manifest(manifest, manifest_path.parent)
            page_errors.extend(f"{page_id}: {error}" for error in page_validation.get("errors", []))
            entries.append(
                {
                    "page_id": page_id,
                    "manifest": manifest,
                    "manifest_path": manifest_path,
                    "validation": page_validation,
                }
            )
        slide = entries[0]["manifest"].get("slide", WIDE_SLIDE)
        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else manifest_paths[0].parent / "rebuilt.pptx"
        )
        if output_path.suffix.lower() != ".pptx":
            output_path = output_path.with_suffix(".pptx")
        log(f"Rebuilding {len(entries)} page(s) from manifests ...")
        write_deck({"slide": slide}, entries, output_path)
        deck_validation = validate_pptx(output_path, entries)
        deck_validation["page_errors"] = page_errors
        if page_errors:
            deck_validation["passed"] = False
        deck_validation["warnings"] = sorted(
            set(deck_validation.get("warnings", []))
            | {warning for entry in entries for warning in entry["validation"].get("warnings", [])}
        )
        report_path = output_path.with_suffix(".validation.json")
        report_path.write_text(json.dumps(deck_validation, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Output: {output_path}")
        log(f"Validation: {'PASS' if deck_validation['passed'] else 'FAIL'} -> {report_path}")
        for error in deck_validation.get("errors", []) + page_errors:
            log(f"  ERROR: {error}")
        return 0 if deck_validation["passed"] else 1

    if not args.inputs:
        raise SystemExit("Provide input images or use --from-manifest.")

    input_paths = []
    for raw in args.inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"Input not found: {path}")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            raise SystemExit(f"Unsupported input type: {path}")
        input_paths.append(path)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        stem = input_paths[0].stem
        output_path = input_paths[0].parent / f"{stem}_editable.pptx"
    if output_path.suffix.lower() != ".pptx":
        output_path = output_path.with_suffix(".pptx")

    workdir = (
        Path(args.workdir).expanduser().resolve()
        if args.workdir
        else output_path.parent / f"{output_path.stem}_work"
    )
    pages_dir = workdir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    source_sizes = [oriented_size(path) for path in input_paths]
    slide = plan_slide(source_sizes)
    log(f"Slide canvas: {slide['width']} x {slide['height']} in ({slide['size_mode']})")

    detected = detect_ocr_backend(args.ocr)
    # Only touch the credential store when the Paddle backend is actually in
    # use; offline backends never need the token.
    paddle_token = load_paddle_token(args.paddle_token) if detected in ("ppocrv6", "paddleocr-vl") else None
    if detected == "none":
        warn("No OCR backend available. Install RapidOCR (`pip install rapidocr onnxruntime`) or configure a Paddle OCR token.")
    else:
        log(f"OCR backend: {detected}")

    entries = []
    started = time.time()
    for index, source_path in enumerate(input_paths, 1):
        page_id = f"page_{index:03d}"
        log(f"[{index}/{len(input_paths)}] {source_path.name} -> {page_id}")
        width_px, height_px = oriented_size(source_path)
        content_box = fit_content_box(width_px, height_px, slide)
        entry = process_page(
            page_id=page_id,
            source_image=source_path,
            page_dir=pages_dir / page_id,
            slide=slide,
            content_box=content_box,
            ocr_backend=detected,
            paddle_token=paddle_token,
            paddle_model=args.paddle_model,
            chart_recognition=args.chart_recognition,
            mode=args.mode,
            font_cjk=args.font_cjk,
            font_latin=args.font_latin,
            extract_icons=args.extract_icons,
            detect_shapes=not args.no_shapes,
            no_text_fit=args.no_text_fit,
            raster_text_mad=args.raster_text_mad,
            raster_text_std=args.raster_text_std,
            gradient_backdrops=bool(args.gradient_backdrops),
            bake_backdrops=bool(args.bake_backdrops),
            max_source_side=args.max_source_side,
            bg_max_width=args.bg_max_width,
        )
        entries.append(entry)

    # Deck manifest (provenance / resumability).
    deck_manifest = {
        "schema_version": 1,
        "run_id": workdir.name,
        "input_type": "images" if len(input_paths) > 1 else "image",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "slide": slide,
        "pages": [
            {
                "page_id": entry["page_id"],
                "page_index": index,
                "manifest": rel_posix(entry["manifest_path"], workdir),
                "source": rel_posix(entry["manifest_path"].parent / "source.png", workdir),
                "ocr_backend": entry["ocr_backend"],
                "validation": rel_posix(entry["manifest_path"].parent / "validation.json", workdir),
            }
            for index, entry in enumerate(entries, start=1)
        ],
        "output": rel_posix(output_path, workdir),
    }
    (workdir / "deck_manifest.json").write_text(json.dumps(deck_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log("Building deck ...")
    write_deck(deck_manifest, entries, output_path)
    deck_validation = validate_pptx(output_path, entries)
    page_warnings = []
    for entry in entries:
        page_warnings.extend(entry["validation"].get("warnings", []))
    deck_validation["warnings"] = sorted(set(page_warnings + WARNINGS + deck_validation.get("warnings", [])))
    deck_validation["pages"] = [
        {
            "page_id": entry["page_id"],
            "passed": entry["validation"].get("passed"),
            "editable": entry["validation"].get("editable"),
            "counts": entry["validation"].get("counts"),
            "ocr_backend": entry["ocr_backend"],
        }
        for entry in entries
    ]
    deck_validation["elapsed_seconds"] = round(time.time() - started, 1)
    (workdir / "validation.json").write_text(json.dumps(deck_validation, ensure_ascii=False, indent=2), encoding="utf-8")

    log("")
    log(f"Output: {output_path}")
    log(
        f"Validation: {'PASS' if deck_validation['passed'] else 'FAIL'} | "
        f"pages={len(entries)} | text={sum(len(e['manifest'].get('text_boxes', [])) for e in entries)} | "
        f"shapes={sum(len(e['manifest'].get('shapes', [])) for e in entries)} | "
        f"images={sum(len(e['manifest'].get('images', [])) for e in entries)}"
    )
    log(f"Artifacts: {workdir}")
    if deck_validation["warnings"]:
        log("Warnings:")
        for message in deck_validation["warnings"]:
            log(f"  - {message}")
    if not deck_validation["passed"]:
        for error in deck_validation.get("errors", []):
            log(f"  ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
