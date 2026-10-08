"""
core/links.py

Hyperlink extraction and cross-language link consistency verification.

Compares hyperlinks of an English master PDF against translated PDFs:
  - QR code links   -> URLs under qr.xylem.com are recognized and normalized across
                       translations so unique destination numbers don't falsely fail.
  - External links  -> Matched by exact URL (http, https, mailto, etc.) and count.
  - Internal links  -> Compared by count; when counts differ, destination anchors and
                       section number prefixes are aligned via SequenceMatcher to pinpoint
                       the exact missing or extra internal links.

No intermediate JSON files are created: all data structures remain in-memory and flow
directly through the SpotCheck inspection pipeline, Excel reporting, and Review tab.
"""

import logging
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

try:
    import pymupdf as fitz
except ImportError:
    import fitz

log = logging.getLogger(__name__)

QR_PATTERN = re.compile(r"https?://qr\.xylem\.com", re.IGNORECASE)
QR_PLACEHOLDER = "https://qr.xylem.com/{QR_CODE}"
QR_LABEL = "QR code link (https://qr.xylem.com/...)"
INTERNAL_LABEL = "internal links (TOC / cross-references) - compared by count only"
DEST_RE = re.compile(r"^page\s+\d+\s+\((.*)\)$")


def get_anchor_text(page: fitz.Page, rect: fitz.Rect) -> str:
    """Return the visible text that sits under a link rectangle."""
    r = fitz.Rect(rect)
    r.x0 += 1
    r.x1 -= 1
    r.y0 += 1
    r.y1 -= 1
    try:
        words = page.get_text("words", clip=r)  # (x0, y0, x1, y1, word, block, line, word_no)
        words.sort(key=lambda w: (round(w[1]), w[0]))  # top-to-bottom, left-to-right
        return " ".join(w[4] for w in words).strip()
    except Exception:
        return ""


def link_target(
    doc: fitz.Document, link: dict[str, Any]
) -> Optional[tuple[str, str, tuple]]:
    """Return (type, target_string, identity_key) for a PyMuPDF link dict, or None to skip."""
    kind = link.get("kind")

    if kind == fitz.LINK_URI:
        uri = link.get("uri", "")
        return "external", uri, ("uri", uri)

    if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
        page = link.get("page", -1)
        name = link.get("nameddest") or link.get("name") or ""

        # Resolve named destinations to actual page number if possible
        if (page is None or page < 0) and name:
            try:
                resolved = doc.resolve_link(name)
                if resolved and resolved[0] is not None and resolved[0] >= 0:
                    page = resolved[0]
            except Exception:
                pass

        if page is not None and page >= 0:
            target = f"page {page + 1}"
            if name:
                target += f" ({name})"
        else:
            target = name
        return "internal", target, ("goto", page, name)

    if kind == fitz.LINK_LAUNCH:
        f = link.get("file", "")
        return "file", f, ("file", f)

    return None


def is_continuation(prev_rect: fitz.Rect, rect: fitz.Rect, line_gap: int = 6) -> bool:
    """True if `rect` looks like the next wrapped line of the link whose last piece is `prev_rect`."""
    gap = rect.y0 - prev_rect.y1
    vertically_adjacent = -2 <= gap <= line_gap
    starts_below = rect.y0 >= prev_rect.y0 + (prev_rect.height * 0.5)
    return vertically_adjacent and starts_below


def is_same_line(a: fitz.Rect, b: fitz.Rect) -> bool:
    """True if two rectangles sit on the same text line (vertical overlap of at least half the height)."""
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap >= 0.5 * min(a.height, b.height)


def section_prefix(topic_code: str) -> str:
    """Return top 2-level numeric prefix, e.g. '6.11' from '6.11.2' or '6.11'."""
    if not topic_code:
        return ""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", topic_code)
    return m.group(1) if m else topic_code


def extract_links(
    pdf_path: Path | str, external_only: bool = False
) -> list[dict[str, Any]]:
    """
    Extract hyperlinks from a PDF and merge multi-line (wrapped) link rectangles into single entries.
    Returns a list of dicts: {'page': int, 'text': str, 'url': str, 'type': str, 'rect': list[float], 'topic': str, 'section': str}.
    """
    pdf_path = Path(pdf_path)
    links: list[dict[str, Any]] = []

    if not pdf_path.exists():
        return links

    # 2D topic finder using visual positions on the page
    topic_finder = None
    fallback_topic_map: dict[int, str] = {}
    try:
        from core.crop_images import extract_topics_with_positions
        topics = extract_topics_with_positions(pdf_path)
        parsed = []
        for t in topics:
            m = re.match(r"^\s*(\d+(?:\.\d+)*)", t.get("title", ""))
            if m:
                parsed.append((int(t.get("start_page", 1)), float(t.get("top_y", 0.0)), m.group(1), t.get("title", "")))
        parsed.sort(key=lambda x: (x[0], x[1]))
        if parsed:
            def _find_topic(page_num: int, y_val: float) -> tuple[str, str]:
                best_code, best_title = "", ""
                for p, top_y, code, title in parsed:
                    if (p < page_num) or (p == page_num and top_y <= y_val + 5):
                        best_code, best_title = code, title
                    elif p > page_num:
                        break
                return best_code, best_title
            topic_finder = _find_topic
    except Exception:
        pass

    if not topic_finder:
        try:
            from core.toc import topic_page_spans
            spans = topic_page_spans(pdf_path)
            for code, (lo, hi) in spans.items():
                for p in range(lo, hi + 1):
                    fallback_topic_map[p] = code
        except Exception:
            pass

    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            groups: list[dict[str, Any]] = []

            try:
                raw_links = page.get_links()
            except Exception:
                raw_links = []

            # Sort links by visual reading order (top-to-bottom, left-to-right)
            raw_links.sort(
                key=lambda l: (round(fitz.Rect(l["from"]).y0, 1), fitz.Rect(l["from"]).x0)
            )

            for link in raw_links:
                info = link_target(doc, link)
                if info is None:
                    continue
                link_type, target, key = info
                if external_only and link_type != "external":
                    continue

                rect = fitz.Rect(link["from"])
                text = get_anchor_text(page, rect)

                # Merge into an earlier group if same target and continuation/same-line fragment
                merged = False
                for g in groups:
                    if g["key"] != key:
                        continue
                    if is_continuation(g["rects"][-1], rect) or any(
                        is_same_line(r, rect) for r in g["rects"]
                    ):
                        g["rects"].append(rect)
                        if text:
                            g["texts"].append(text)
                        merged = True
                        break

                if not merged:
                    groups.append(
                        {
                            "key": key,
                            "type": link_type,
                            "target": target,
                            "rects": [rect],
                            "texts": [text] if text else [],
                        }
                    )

            for g in groups:
                first_r = g["rects"][0] if g["rects"] else None
                rect_coords = [round(first_r.x0, 1), round(first_r.y0, 1),
                               round(first_r.x1, 1), round(first_r.y1, 1)] if first_r else None
                y_pos = float(first_r.y0) if first_r else 0.0
                if topic_finder:
                    t_code, _ = topic_finder(page_index, y_pos)
                else:
                    t_code = fallback_topic_map.get(page_index, "")
                s_code = section_prefix(t_code)

                links.append(
                    {
                        "page": page_index,
                        "text": " ".join(g["texts"]).strip(),
                        "url": g["target"],
                        "type": g["type"],
                        "rect": rect_coords,
                        "topic": t_code,
                        "section": s_code,
                    }
                )
    return links


def is_qr_link(url: str) -> bool:
    """Check if the URL is a QR code link (e.g. qr.xylem.com)."""
    return bool(QR_PATTERN.search(url.strip()))


def link_key(
    link: dict[str, Any],
    normalize: bool = True,
) -> tuple[str, str]:
    """Language-independent identity of a link."""
    if link["type"] == "internal":
        return ("internal", "")

    target = link["url"].strip()
    if normalize and is_qr_link(target):
        return ("qr", QR_PLACEHOLDER)

    return (link["type"], target)


def describe_key(key: tuple[str, str]) -> str:
    """Human-readable description of a link key."""
    kind, target = key
    if kind == "internal":
        return INTERNAL_LABEL
    if kind == "qr":
        return QR_LABEL
    return target


def parse_dest(url: str) -> str:
    """Extract named destination from an internal link URL like 'page 6 (dest_name)' or 'dest_name'."""
    url = url.strip()
    m = DEST_RE.match(url)
    if m:
        return m.group(1)
    if url and not url.startswith(("http://", "https://", "mailto:", "ftp://", "file:")) and not url.startswith("page "):
        return url
    return ""


def num_prefix(text: str) -> str:
    """Leading section number of a link text, e.g. '6.3.3' from '6.3.3 Control...'."""
    m = re.match(r"\s*(\d+(?:\.\d+)*)\b", text)
    return m.group(1) if m else ""


def brief(link: dict[str, Any]) -> dict[str, Any]:
    """Compact representation of a link for diff output."""
    text = re.sub(r"\.{3,}.*$", "", link.get("text", "")).strip()
    return {
        "page": link.get("page", 1),
        "text": text[:100],
        "url": link.get("url", ""),
        "rect": link.get("rect"),
        "topic": link.get("topic", ""),
        "section": link.get("section", ""),
    }


def neighbors(seq: list[dict[str, Any]], i: int) -> list[dict[str, Any]]:
    """Return the links just before and at position `i` for context."""
    return [brief(seq[k]) for k in (i - 1, i) if 0 <= k < len(seq)]


def diff_internal(
    en: list[dict[str, Any]], tr: list[dict[str, Any]]
) -> dict[str, list]:
    """Align the internal links of both PDFs (document order) and return the exact missing / extra ones."""
    en_names = {parse_dest(link["url"]) for link in en} - {""}
    tr_names = {parse_dest(link["url"]) for link in tr} - {""}
    shared = en_names & tr_names

    def token(link: dict[str, Any]) -> tuple[str, str, str]:
        name = parse_dest(link["url"])
        sec = link.get("section") or section_prefix(link.get("topic", ""))
        if name in shared:
            return ("N", name, sec)
        return ("T", num_prefix(link.get("text", "")), sec)

    a = [token(link) for link in en]
    b = [token(link) for link in tr]

    raw_missing: list[dict] = []
    raw_extra: list[dict] = []
    changed: list[dict] = []

    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        uncertain = (op == "replace")
        n = min(i2 - i1, j2 - j1) if op == "replace" else 0
        for k in range(n):
            changed.append({"english": brief(en[i1 + k]), "translated": brief(tr[j1 + k])})
        for i in range(i1 + n, i2):
            raw_missing.append({
                **brief(en[i]),
                "translated_neighbors": neighbors(tr, j2 if uncertain else j1),
                "uncertain_position": uncertain,
            })
        for j in range(j1 + n, j2):
            raw_extra.append({
                **brief(tr[j]),
                "english_neighbors": neighbors(en, i2 if uncertain else i1),
                "uncertain_position": uncertain,
            })

    # Multiset quotas prevent local reordering / permutations on a page from causing
    # false positives (e.g. adjacent table columns or wrapped lines swapped in reading order).
    en_dest_counts = Counter(parse_dest(l["url"]) for l in en if parse_dest(l["url"]))
    tr_dest_counts = Counter(parse_dest(l["url"]) for l in tr if parse_dest(l["url"]))
    en_sec_counts = Counter(num_prefix(l.get("text", "")) for l in en if num_prefix(l.get("text", "")))
    tr_sec_counts = Counter(num_prefix(l.get("text", "")) for l in tr if num_prefix(l.get("text", "")))

    missing_dest_quota = {k: max(0, en_dest_counts[k] - tr_dest_counts.get(k, 0)) for k in en_dest_counts}
    missing_sec_quota = {k: max(0, en_sec_counts[k] - tr_sec_counts.get(k, 0)) for k in en_sec_counts}
    extra_dest_quota = {k: max(0, tr_dest_counts[k] - en_dest_counts.get(k, 0)) for k in tr_dest_counts}
    extra_sec_quota = {k: max(0, tr_sec_counts[k] - en_sec_counts.get(k, 0)) for k in tr_sec_counts}

    missing: list[dict] = []
    for m in raw_missing:
        d = parse_dest(m["url"])
        sec = num_prefix(m.get("text", ""))
        if d in shared:
            if missing_dest_quota.get(d, 0) > 0:
                missing.append(m)
                missing_dest_quota[d] -= 1
        elif sec:
            if missing_sec_quota.get(sec, 0) > 0:
                missing.append(m)
                missing_sec_quota[sec] -= 1
        else:
            missing.append(m)

    extra: list[dict] = []
    for e in raw_extra:
        d = parse_dest(e["url"])
        sec = num_prefix(e.get("text", ""))
        if d in shared:
            if extra_dest_quota.get(d, 0) > 0:
                extra.append(e)
                extra_dest_quota[d] -= 1
        elif sec:
            if extra_sec_quota.get(sec, 0) > 0:
                extra.append(e)
                extra_sec_quota[sec] -= 1
        else:
            extra.append(e)

    return {
        "missing_internal_links": missing,
        "extra_internal_links": extra,
        "changed_internal_links": changed,
    }


def group_by_key(
    links: list[dict[str, Any]],
    normalize: bool = True,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group links by their language-independent identity key."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        groups[link_key(link, normalize=normalize)].append(link)
    return dict(groups)


def split_by_type(links: list[dict[str, Any]]) -> dict[str, int]:
    """Break down link counts by type (external, internal, file)."""
    counts: dict[str, int] = {}
    for link in links:
        t = link.get("type", "unknown")
        counts[t] = counts.get(t, 0) + 1
    return counts


def compare_links(
    en_links: list[dict[str, Any]],
    tr_links: list[dict[str, Any]],
    en_id: Optional[str] = None,
    tr_id: Optional[str] = None,
    normalize: bool = True,
    en_path: Optional[str] = None,
    tr_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Compare English vs translated link sets and return a comprehensive result dict.
    Does NOT write any JSON files.
    """
    en_groups = group_by_key(en_links, normalize=normalize)
    tr_groups = group_by_key(tr_links, normalize=normalize)
    en_count = Counter({k: len(v) for k, v in en_groups.items()})
    tr_count = Counter({k: len(v) for k, v in tr_groups.items()})

    missing: list[dict] = []
    extra: list[dict] = []

    for key in en_count:
        diff = en_count[key] - tr_count.get(key, 0)
        if diff > 0:
            sample = en_groups[key][0]
            missing.append({
                "type": key[0],
                "target": describe_key(key),
                "missing_count": diff,
                "english_count": en_count[key],
                "translated_count": tr_count.get(key, 0),
                "english_text": "" if key[0] == "internal" else sample.get("text", ""),
                "english_pages": [] if key[0] == "internal" else sorted({l["page"] for l in en_groups[key]}),
            })

    for key in tr_count:
        diff = tr_count[key] - en_count.get(key, 0)
        if diff > 0:
            sample = tr_groups[key][0]
            extra.append({
                "type": key[0],
                "target": describe_key(key),
                "extra_count": diff,
                "english_count": en_count.get(key, 0),
                "translated_count": tr_count[key],
                "translated_text": "" if key[0] == "internal" else sample.get("text", ""),
                "translated_pages": [] if key[0] == "internal" else sorted({l["page"] for l in tr_groups[key]}),
            })

    en_int = [l for l in en_links if l.get("type") == "internal"]
    tr_int = [l for l in tr_links if l.get("type") == "internal"]
    internal_diff = diff_internal(en_int, tr_int) if len(en_int) != len(tr_int) else None

    count_match = (len(en_links) == len(tr_links))
    targets_match = (not missing and not extra)
    verdict = "PASS" if (count_match and targets_match) else "FAIL"

    return {
        "english_pdf": en_id or "",
        "translated_pdf": tr_id or "",
        "english_pdf_path": str(en_path or en_id or ""),
        "translated_pdf_path": str(tr_path or tr_id or ""),
        "english_total": len(en_links),
        "translated_total": len(tr_links),
        "english_breakdown": split_by_type(en_links),
        "translated_breakdown": split_by_type(tr_links),
        "count_match": count_match,
        "targets_match": targets_match,
        "status": verdict,
        "missing_in_translated": missing,
        "extra_in_translated": extra,
        "internal_link_diff": internal_diff,
    }
