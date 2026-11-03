"""Pull figures out of a PDF as pictures, keeping the arrows and labels drawn over them.

"Extract images" tools copy the stored photo only, so arrows, leader lines and
labels (drawn on top as separate lines and text) are lost. Here each photo's box on
the page is found, grown to take in the labels and lines that touch it, and that
page area is rendered as it looks. No AI involved: the PDF itself records where
every picture, line and word sits.
"""
import io
import re
import time
import zipfile

import pymupdf

MARGINS = {"tight": 4, "normal": 12, "wide": 40}  # extra points (1/72 inch) around each figure
MIN_SIDE = 50  # points; smaller pictures are icons, bullets or logos
LABEL_GAP = 8  # points; text or lines this close to a figure belong to it
LABEL_MAX_WIDTH = 0.45  # of the page width; longer text lines are body text, not labels
DPI = 200
MAX_SIDE_PX = 3000
TIME_BUDGET = 20  # seconds; the web server gives up on a request after 30


class FigureError(ValueError):
    pass


def parse_pages(text, page_count):
    """'12-30, 45' -> 0-based page indices; blank means every page."""
    text = (text or "").strip()
    if not text:
        return list(range(page_count))
    pages = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise FigureError(f"“{part}” is not a page or range. Write pages like 12-30, 45.")
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if first < 1 or last < first:
            raise FigureError(f"“{part}” is not a valid page range.")
        if first > page_count:
            raise FigureError(f"This PDF has only {page_count} pages.")
        pages += range(first - 1, min(last, page_count))
    return sorted(set(pages))


def _merge(rects, gap=4):
    """Join boxes that touch, so a figure stored as several tiles comes out as one picture."""
    rects = [pymupdf.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(rects)):
            grown = rects[i] + (-gap, -gap, gap, gap)
            for j in range(i + 1, len(rects)):
                if grown.intersects(rects[j]):
                    rects[i] |= rects.pop(j)
                    merged = True
                    break
            if merged:
                break
    return rects


def _touches(rect, box):
    # Written out because PyMuPDF treats a flat rectangle (a straight line) as empty.
    return rect.x0 <= box.x1 and rect.x1 >= box.x0 and rect.y0 <= box.y1 and rect.y1 >= box.y0


def _union(a, b):
    # Also written out: PyMuPDF's own union skips flat rectangles.
    return pymupdf.Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


def _with_labels(box, lines, labels):
    """Grow a figure box over the leader lines and label text that touch it (twice, for labels at line ends)."""
    for _ in range(2):
        near = box + (-LABEL_GAP, -LABEL_GAP, LABEL_GAP, LABEL_GAP)
        for rect in lines:
            if _touches(rect, near) and rect.width <= box.width * 1.5 and rect.height <= box.height * 1.5:
                box = _union(box, rect)
        for rect in labels:
            if _touches(rect, near):
                box = _union(box, rect)
    return box


def figure_rects(page, margin):
    boxes = [pymupdf.Rect(info["bbox"]) & page.rect for info in page.get_image_info()]
    boxes = [box for box in boxes if box.width >= MIN_SIDE and box.height >= MIN_SIDE]
    if not boxes:
        return []
    lines = [pymupdf.Rect(d["rect"]) for d in page.get_drawings() if "s" in (d.get("type") or "")]
    labels = [
        pymupdf.Rect(line["bbox"])
        for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
        if "".join(span["text"] for span in line["spans"]).strip()
        and line["bbox"][2] - line["bbox"][0] <= page.rect.width * LABEL_MAX_WIDTH
    ]
    grown = _merge([_with_labels(box, lines, labels) for box in _merge(boxes)], gap=0)
    rects = [(box + (-margin, -margin, margin, margin)) & page.rect for box in grown]
    return sorted(rects, key=lambda r: (round(r.y0 / 20), r.x0))


def _render(page, clip):
    zoom = min(DPI / 72, MAX_SIDE_PX / max(clip.width, clip.height))
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pixmap.tobytes("jpeg", jpg_quality=88)


def extract(source, pages="", margin="normal", whole_pages=False, time_budget=TIME_BUDGET):
    """Return (zip bytes, file name, figure count, first page not done or None).

    ``source`` is a file path or the PDF's bytes. Files are named p012_fig1.jpg
    (page 12, first figure from the top), or p012.jpg for whole pages.
    """
    try:
        doc = pymupdf.open(source) if isinstance(source, str) else pymupdf.open(stream=source, filetype="pdf")
    except Exception as error:  # PyMuPDF raises several types for damaged files
        raise FigureError("This file could not be opened as a PDF.") from error
    if doc.needs_pass:
        raise FigureError("This PDF is password-protected. Remove the password and try again.")
    indices = parse_pages(pages, doc.page_count)
    pad = MARGINS.get(margin, MARGINS["normal"])
    started = time.monotonic()
    buffer, count, stopped_at = io.BytesIO(), 0, None
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:  # JPEGs are already compressed
        for index in indices:
            if count and time.monotonic() - started > time_budget:
                stopped_at = index + 1
                break
            page = doc[index]
            if whole_pages:
                archive.writestr(f"p{index + 1:03d}.jpg", _render(page, page.rect))
                count += 1
                continue
            for number, rect in enumerate(figure_rects(page, pad), 1):
                archive.writestr(f"p{index + 1:03d}_fig{number}.jpg", _render(page, rect))
                count += 1
        if stopped_at:
            archive.writestr("CONTINUE_FROM_PAGE.txt", (
                f"The time limit was reached, so this zip stops before page {stopped_at}.\n"
                f"Run Image processing again with pages {stopped_at}-{indices[-1] + 1} to get the rest.\n"))
    doc.close()
    if not count:
        raise FigureError(
            "No pictures were found on these pages. If the figures are drawings rather than photos, "
            "choose “Whole pages” and crop them instead.")
    last_done = (stopped_at - 1) if stopped_at else indices[-1] + 1
    name = f"figures_p{indices[0] + 1}-{last_done}.zip"
    return buffer.getvalue(), name, count, stopped_at
