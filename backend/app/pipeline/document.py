"""PDF analysis and a layout-aware document model shared by text-layer and OCR extraction.

Coordinates are normalised to PDF points (1/72 inch) with the origin at the top-left of the page,
so the extractor does not care whether words came from the text layer or from Tesseract.
"""
from __future__ import annotations

import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber
from PIL import Image, ImageFilter, ImageOps

from ..config import OCR_DPI

MIN_TEXT_CHARS_PER_PAGE = 40     # below this a page is treated as scanned
SEGMENT_GAP_PT = 14.0            # horizontal gap that separates two "cells" on the same line


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: float = 1.0           # 0..1, 1.0 for text-layer words
    size: float = 0.0           # font size (text layer) or glyph height (OCR)


@dataclass
class Segment:
    words: list[Word]
    page: int
    line_idx: int

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self): return min(w.x0 for w in self.words)

    @property
    def x1(self): return max(w.x1 for w in self.words)

    @property
    def top(self): return min(w.top for w in self.words)

    @property
    def bottom(self): return max(w.bottom for w in self.words)

    @property
    def conf(self): return statistics.fmean(w.conf for w in self.words) if self.words else 0.0

    @property
    def size(self): return max(w.size for w in self.words) if self.words else 0.0

    def bbox(self):
        return [round(self.x0, 1), round(self.top, 1), round(self.x1, 1), round(self.bottom, 1)]


@dataclass
class Line:
    words: list[Word]
    page: int
    idx: int
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self): return min(w.top for w in self.words)

    @property
    def conf(self): return statistics.fmean(w.conf for w in self.words) if self.words else 0.0


@dataclass
class DocModel:
    pages: int
    page_sizes: list[tuple[float, float]]
    lines: list[Line]
    method: str                         # "text-layer" or "ocr"
    ocr_mean_conf: Optional[float] = None
    page_text_chars: list[int] = field(default_factory=list)
    deskew_degrees: list[float] = field(default_factory=list)

    @property
    def segments(self) -> list[Segment]:
        return [s for ln in self.lines for s in ln.segments]

    @property
    def full_text(self) -> str:
        out, page = [], None
        for ln in self.lines:
            if ln.page != page:
                page = ln.page
                out.append(f"--- page {page} ---")
            out.append(ln.text)
        return "\n".join(out)


def _group_lines(words: list[Word], page: int, start_idx: int, y_tol: float) -> list[Line]:
    words = sorted(words, key=lambda w: (round(w.top), w.x0))
    rows: list[list[Word]] = []
    for w in words:
        mid = (w.top + w.bottom) / 2
        for row in rows:
            rmid = statistics.fmean((x.top + x.bottom) / 2 for x in row)
            if abs(rmid - mid) <= y_tol:
                row.append(w)
                break
        else:
            rows.append([w])
    rows.sort(key=lambda r: min(w.top for w in r))
    lines = []
    for i, row in enumerate(rows):
        row.sort(key=lambda w: w.x0)
        ln = Line(words=row, page=page, idx=start_idx + i)
        seg = [row[0]]
        for prev, cur in zip(row, row[1:]):
            if cur.x0 - prev.x1 > SEGMENT_GAP_PT:
                ln.segments.append(Segment(seg, page, ln.idx))
                seg = []
            seg.append(cur)
        ln.segments.append(Segment(seg, page, ln.idx))
        lines.append(ln)
    return lines


def analyze_pdf(path: Path) -> dict:
    """Stage 2: what kind of document is this?"""
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        chars = [len(p.chars) for p in pdf.pages]
        images = [len(p.images) for p in pdf.pages]
        sizes = [(float(p.width), float(p.height)) for p in pdf.pages]
    scanned_pages = [i + 1 for i, c in enumerate(chars) if c < MIN_TEXT_CHARS_PER_PAGE]
    return dict(page_count=n, text_chars_per_page=chars, images_per_page=images, page_sizes=sizes,
                scanned_pages=scanned_pages, is_scanned=len(scanned_pages) == n,
                needs_ocr=bool(scanned_pages))


def text_layer_doc(path: Path) -> DocModel:
    lines: list[Line] = []
    sizes = []
    chars = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            sizes.append((float(page.width), float(page.height)))
            chars.append(len(page.chars))
            ws = page.extract_words(extra_attrs=["size"], keep_blank_chars=False, x_tolerance=1.5)
            words = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], 1.0, float(w.get("size", 0)))
                     for w in ws]
            lines += _group_lines(words, pno, len(lines), y_tol=2.5)
    return DocModel(pages=len(sizes), page_sizes=sizes, lines=lines, method="text-layer", page_text_chars=chars)


def estimate_skew(img: Image.Image) -> float:
    """Projection-profile deskew: the rotation that makes text rows sharpest (max variance of row ink)."""
    small = img.convert("L").resize((img.width // 4, img.height // 4))
    small = small.point(lambda v: 255 if v < 140 else 0)       # ink = white for summing
    best, best_score = 0.0, -1.0
    for tenth in range(-30, 31, 2):                               # -3.0 .. +3.0 degrees
        ang = tenth / 10
        rot = small.rotate(ang, resample=Image.NEAREST, fillcolor=0)
        w, h = rot.size
        data = rot.tobytes()
        rows = [sum(data[r * w:(r + 1) * w]) for r in range(h)]
        mean = sum(rows) / h
        score = sum((x - mean) ** 2 for x in rows)
        if score > best_score:
            best, best_score = ang, score
    return best


def preprocess_for_ocr(img: Image.Image) -> tuple[Image.Image, float]:
    """Deskew, denoise and binarise a scanned page.
    Median filter removes speckle, autocontrast normalises paper tint, fixed threshold binarises."""
    g = img.convert("L")
    angle = estimate_skew(g)
    if abs(angle) >= 0.2:
        g = g.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    g = g.filter(ImageFilter.MedianFilter(3))
    g = ImageOps.autocontrast(g, cutoff=1)
    return g.point(lambda v: 255 if v > 150 else 0), angle


def ocr_doc(path: Path, dpi: int = OCR_DPI) -> DocModel:
    import pytesseract

    scale = 72.0 / dpi
    lines: list[Line] = []
    sizes, confs, skews = [], [], []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", str(path), str(Path(td) / "p")],
                       check=True, capture_output=True, timeout=120)
        for pno, png in enumerate(sorted(Path(td).glob("p*.png")), start=1):
            img, angle = preprocess_for_ocr(Image.open(png))
            skews.append(angle)
            sizes.append((img.width * scale, img.height * scale))
            data = pytesseract.image_to_data(img, config="--psm 4", output_type=pytesseract.Output.DICT)
            words = []
            for i, txt in enumerate(data["text"]):
                txt = (txt or "").strip()
                if not txt:
                    continue
                conf = float(data["conf"][i])
                if conf < 0:
                    continue
                x0, top = data["left"][i] * scale, data["top"][i] * scale
                x1, bottom = x0 + data["width"][i] * scale, top + data["height"][i] * scale
                words.append(Word(txt, x0, x1, top, bottom, conf / 100.0, bottom - top))
                confs.append(conf / 100.0)
            lines += _group_lines(words, pno, len(lines), y_tol=4.0)
    return DocModel(pages=len(sizes), page_sizes=sizes, lines=lines, method="ocr",
                    ocr_mean_conf=round(statistics.fmean(confs), 3) if confs else 0.0, deskew_degrees=skews)


def render_page_png(path: Path, page: int = 1, dpi: int = 110) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "pg"
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-f", str(page), "-l", str(page), "-singlefile",
                        str(path), str(out)], check=True, capture_output=True, timeout=60)
        return (Path(td) / "pg.png").read_bytes()
