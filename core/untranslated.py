"""
core/untranslated.py

Find English text left behind in a translated manual.

TWO TESTS, AND ONLY ONE OF THEM IS TRUSTWORTHY ALONE
----------------------------------------------------
VERBATIM - the line appears word for word in the English master. This is the
strong one. A translator does not reproduce an English sentence by accident, so
a body line that survives into the Danish copy unchanged was missed. Measured
against the shipped Start 350 set: 215 master lines of four words or more, and
the test fires on NOTHING in any of the eleven translations.

DENSE - the line reads as English by its function words (the, of, with, must,
see). On its own this is noise. "for" is also Danish and Norwegian, "in" is
Italian, "of" and "is" are Dutch: run the density test by itself and eleven
clean manuals produce eighteen findings, every one of them wrong. It earns its
place only as corroboration, or for catching English that is NOT in the master
at all - a sentence pasted in from a previous revision, say.

Either test alone flags the line; both together make it certain.

NUMERALS ARE IGNORED, deliberately. A dimensions line reads the same in every
language, and "300 x 90 x 87 mm (11,8 x 3,5 x 3,4 in)" was the only false
positive left standing once the verbatim test was in place - flagged because
"in" is both an English preposition and an inch. Anything where the digits
dominate is not a translation unit and is skipped outright.

Left alone on purpose: brand names, product names, part numbers, unit symbols
and standards references. "Flygt", "Start 350", "IP68", "EN 809" are SUPPOSED
to survive translation, and the four-word minimum keeps all of them out.

The first and last page are skipped: the cover and the back matter carry
addresses, trademarks and a copyright line that are English by design.
"""

import os
import re
import unicodedata

import pymupdf as fitz


# Function words carry no product meaning, so a translator always replaces
# them. Their density is what makes a line read as English, unlike nouns.
ENGLISH_FUNCTION_WORDS = frozenset("""
the and of to in is are for with on this that be not or from by as it if can
must should will when before after which these those all any have has been
into other than then there use used using see note only each such both while
during between above below over under about
""".split())

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this a line is a heading fragment, a code or a label, and no test can
# tell a missed translation from a proper noun.
MIN_WORDS = 4

# The density test needs a longer line before it means anything, because on a
# short one a single shared preposition is the whole score.
DENSE_MIN_WORDS = 6
DENSE_MIN_RATIO = 0.40

# A line this full of digits is a measurement, a part number or a table cell.
MAX_DIGIT_SHARE = 0.20


def normalise(text):
    """Compare on words alone: case, punctuation and odd spaces do not count."""
    folded = unicodedata.normalize("NFKC", " ".join((text or "").split())).lower()
    return re.sub(r"[^\w\s]", "", folded)


def _lines(page, clip=None):
    out = []
    kwargs = {"clip": fitz.Rect(clip)} if clip else {}
    for block in page.get_text("dict", **kwargs).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if text:
                out.append((text, tuple(float(v) for v in line["bbox"])))
    return out


def is_numeric_line(text):
    """A measurement or a code, not a sentence anyone translates."""
    if not text:
        return True
    digits = sum(c.isdigit() for c in text)
    return digits / max(1, len(text)) > MAX_DIGIT_SHARE


def english_density(text):
    """(share of words that are English function words, word count)."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if not words:
        return 0.0, 0
    hits = sum(1 for w in words if w in ENGLISH_FUNCTION_WORDS)
    return hits / len(words), len(words)


def master_inventory(master_pdf, skip_first_last=True):
    """
    Every line of the English master worth comparing against.

    Normalised, and only lines of MIN_WORDS or more - a shorter one cannot tell
    a missed translation from a product name.
    """
    inventory = set()
    if not master_pdf or not os.path.isfile(master_pdf):
        return inventory
    with fitz.open(master_pdf) as doc:
        total = len(doc)
        for page in doc:
            page_no = page.number + 1
            if skip_first_last and (page_no == 1 or page_no == total):
                continue
            for text, _bbox in _lines(page):
                if is_numeric_line(text):
                    continue
                norm = normalise(text)
                if len(norm.split()) >= MIN_WORDS:
                    inventory.add(norm)
    return inventory


def classify(text, inventory):
    """
    Is this line untranslated English? Returns (flagged, reason).

    reason is one of: "verbatim", "dense", "verbatim + dense", or "".
    """
    if is_numeric_line(text):
        return False, ""
    ratio, words = english_density(text)
    if words < MIN_WORDS:
        return False, ""
    verbatim = bool(inventory) and normalise(text) in inventory and ratio > 0
    dense = words >= DENSE_MIN_WORDS and ratio >= DENSE_MIN_RATIO
    if verbatim and dense:
        return True, "verbatim + dense"
    if verbatim:
        return True, "verbatim"
    if dense:
        return True, "dense"
    return False, ""


def evidence_crop(page, rect, out_path, pad=4.0, dpi=200):
    """A picture of the line itself, so the reviewer reads it rather than a path."""
    try:
        r = fitz.Rect(rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)
        r = r & page.rect
        if r.is_empty:
            return ""
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        page.get_pixmap(clip=r, dpi=dpi).save(out_path)
        return out_path
    except Exception:
        return ""


def find_untranslated(pdf_path, inventory, skip_first_last=True,
                      evidence_dir=None, progress=None):
    """
    Every line of one translation that still reads as English.

    Returns a list of dicts:
        document, path, page, text, reason, density, rect, image
    """
    findings = []
    if not pdf_path or not os.path.isfile(pdf_path):
        return findings

    name = os.path.basename(pdf_path)
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        for page in doc:
            page_no = page.number + 1
            if skip_first_last and (page_no == 1 or page_no == total):
                continue
            if progress:
                progress(page_no, total, name)
            for text, bbox in _lines(page):
                flagged, reason = classify(text, inventory)
                if not flagged:
                    continue
                ratio, _words = english_density(text)
                image = ""
                if evidence_dir:
                    stem = f"{os.path.splitext(name)[0]}_p{page_no}_{len(findings) + 1}.png"
                    image = evidence_crop(page, bbox,
                                          os.path.join(evidence_dir, stem))
                findings.append({
                    "document": name,
                    "path": pdf_path,
                    "page": page_no,
                    "text": text.strip(),
                    "reason": reason,
                    "density": round(ratio, 2),
                    "rect": [round(v, 2) for v in bbox],
                    "image": image,
                })
    return findings


def scan_documents(master_pdf, translated_paths, skip_first_last=True,
                   evidence_dir=None, progress=None):
    """
    Check every translation against the master.

    The master itself is never scanned - it is supposed to be in English.
    """
    inventory = master_inventory(master_pdf, skip_first_last=skip_first_last)
    findings = []
    for path in translated_paths or []:
        if master_pdf and os.path.abspath(path) == os.path.abspath(master_pdf):
            continue
        findings.extend(find_untranslated(
            path, inventory, skip_first_last=skip_first_last,
            evidence_dir=evidence_dir, progress=progress))
    return findings