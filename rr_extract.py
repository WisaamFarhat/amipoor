#!/usr/bin/env python3
"""
ITU Radio Regulations — provision-level extractor (v0)
=======================================================
Goal: turn the RR Articles PDF into provision-keyed records that double as
RAG chunks AND verifiable citations:

    { "article": "9", "provision": "9.21", "title": "...",
      "body": "<verbatim text>", "page": 142, "edition": "2020", "volume": 1 }

Design principle: the provision NUMBER is found by POSITION (left margin),
not by regex alone — so "see No. 9.21" mid-sentence (a cross-reference) is
never mistaken for the START of provision 9.21.

HOW TO USE
----------
1. Put the RR Articles PDF somewhere and set PDF_PATH.
2. Run:  python3 rr_extract.py --inspect      # prints layout stats first
3. Tune the CONFIG thresholds using what --inspect tells you.
4. Run:  python3 rr_extract.py --article 9    # extract just Article 9
5. Check the validation report at the end (sequence gaps = extraction bugs).
6. Output: rr_provisions.jsonl  (one JSON record per line)

The thresholds in CONFIG are STARTING GUESSES. The whole game is tuning them
against real output — that's why --inspect exists. Don't trust the defaults.
"""

import argparse, json, re, sys, statistics
from collections import defaultdict

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber --break-system-packages")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — these are the knobs you tune against the real PDF.
# Run --inspect first; it tells you what real numbers to put here.
# ─────────────────────────────────────────────────────────────────────────────
CONFIG = {
    "PDF_PATH": "/mnt/user-data/uploads/rr_articles.pdf",  # <-- set this
    "EDITION": "2020",
    "VOLUME": 1,

    # A provision number (e.g. "9.21") is only a PROVISION START if its left
    # edge (x0) is within this band — i.e. it sits in the left margin column.
    # --inspect prints the x0 distribution so you can set this correctly.
    "MARGIN_X0_MAX": 90,   # tokens starting left of this x are "margin" tokens

    # Body text font size (pts). Footnotes are smaller. --inspect shows the
    # font-size histogram; set FOOTNOTE_MAX just below the body size.
    "BODY_SIZE_MIN": 8.5,    # text >= this is treated as body
    "FOOTNOTE_MAX": 8.0,     # text <= this is treated as footnote/marginalia

    # Vertical zone (fraction of page height) below which small text is almost
    # certainly a footnote. 0.85 = bottom 15% of page.
    "FOOTNOTE_ZONE_TOP": 0.85,

    # Page range to scan for a given article can be limited once you know it,
    # to speed iteration. None = whole document.
    "PAGE_RANGE": None,   # e.g. (130, 175)
}

# Provision number: ARTICLE.PARA optionally .SUB  e.g. 9.21, 9.21.1, 11.31A
PROV_RE = re.compile(r'^(\d{1,2})\.(\d{1,3}[A-Z]?)(?:\.(\d{1,2}))?$')
# Article heading: "ARTICLE 9" (case-insensitive, allow surrounding space)
ARTICLE_RE = re.compile(r'^ARTICLE\s+(\d{1,2})\b', re.IGNORECASE)


def words_with_pos(page):
    """Return words with x0, top, size, fontname. extra_attrs gives us size."""
    return page.extract_words(
        x_tolerance=1.5, y_tolerance=2,
        keep_blank_chars=False,
        extra_attrs=["size", "fontname"],
    )


def line_group(words, y_tol=2.5):
    """Group words into visual lines by their 'top' coordinate."""
    lines = []
    cur, cur_top = [], None
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if cur_top is None or abs(w["top"] - cur_top) <= y_tol:
            cur.append(w); cur_top = w["top"] if cur_top is None else cur_top
        else:
            lines.append(cur); cur = [w]; cur_top = w["top"]
    if cur:
        lines.append(cur)
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# INSPECT MODE — run this FIRST. It tells you what the real thresholds are.
# ─────────────────────────────────────────────────────────────────────────────
def inspect(pdf_path, sample_pages=8):
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        print(f"Pages: {n}")
        sizes, x0s, prov_x0s = [], [], []
        pages = pdf.pages[:sample_pages]
        for p in pages:
            for w in words_with_pos(p):
                sizes.append(round(w.get("size", 0), 1))
                x0s.append(round(w["x0"]))
                if PROV_RE.match(w["text"]):
                    prov_x0s.append(round(w["x0"]))
        print("\n--- FONT SIZE histogram (body vs footnote separation) ---")
        hist = defaultdict(int)
        for s in sizes: hist[s] += 1
        for s in sorted(hist):
            print(f"  size {s:>5}: {'#'*min(60,hist[s]//5)} {hist[s]}")
        print("\n--- LEFT-EDGE (x0) of tokens that LOOK like provision numbers ---")
        if prov_x0s:
            print(f"  count={len(prov_x0s)} min={min(prov_x0s)} "
                  f"median={int(statistics.median(prov_x0s))} max={max(prov_x0s)}")
            print("  -> set MARGIN_X0_MAX a bit above the median of the LEFT cluster")
        else:
            print("  none found in sample — widen sample_pages or check PDF text layer")
        print("\nNow set CONFIG.MARGIN_X0_MAX, BODY_SIZE_MIN, FOOTNOTE_MAX accordingly.")


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────
def is_footnote(word, page_height):
    size = word.get("size", 99)
    in_zone = word["top"] >= page_height * CONFIG["FOOTNOTE_ZONE_TOP"]
    return size <= CONFIG["FOOTNOTE_MAX"] or (in_zone and size < CONFIG["BODY_SIZE_MIN"])


def extract(pdf_path, only_article=None):
    records = []
    cur = None          # current provision being accumulated
    cur_article = None

    def flush():
        nonlocal cur
        if cur and cur["body"].strip():
            cur["body"] = re.sub(r'\s+', ' ', cur["body"]).strip()
            records.append(cur)
        cur = None

    with pdfplumber.open(pdf_path) as pdf:
        rng = CONFIG["PAGE_RANGE"]
        pages = pdf.pages[rng[0]:rng[1]] if rng else pdf.pages
        base = (rng[0] if rng else 0)

        for pno, page in enumerate(pages, start=base + 1):
            ph = page.height
            words = [w for w in words_with_pos(page) if not is_footnote(w, ph)]
            for line in line_group(words):
                line_sorted = sorted(line, key=lambda w: w["x0"])
                first = line_sorted[0]
                text = " ".join(w["text"] for w in line_sorted).strip()

                # Article boundary?
                am = ARTICLE_RE.match(text)
                if am:
                    flush()
                    cur_article = am.group(1)
                    continue

                if only_article and cur_article != str(only_article):
                    continue

                # Provision start? FIRST token must look like a provision number
                # AND sit in the left margin (position test — this is the crux).
                pm = PROV_RE.match(first["text"])
                if pm and first["x0"] <= CONFIG["MARGIN_X0_MAX"]:
                    flush()
                    prov = first["text"]
                    rest = " ".join(w["text"] for w in line_sorted[1:]).strip()
                    cur = {
                        "article": cur_article or pm.group(1),
                        "provision": prov,
                        "title": "",          # filled if next text looks like a heading
                        "body": rest,
                        "page": pno,
                        "edition": CONFIG["EDITION"],
                        "volume": CONFIG["VOLUME"],
                    }
                else:
                    # Continuation of current provision (incl. across page breaks)
                    if cur is not None:
                        cur["body"] += " " + text
        flush()
    return records


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATE — your domain knowledge as the QA oracle. Gaps = extraction bugs.
# ─────────────────────────────────────────────────────────────────────────────
def validate(records):
    print(f"\n=== VALIDATION ===\nExtracted {len(records)} provisions")
    by_article = defaultdict(list)
    for r in records:
        by_article[r["article"]].append(r["provision"])

    for art in sorted(by_article, key=lambda a: int(a) if a.isdigit() else 999):
        provs = by_article[art]
        # parse the middle number (paragraph) for gap detection
        def para(p):
            m = PROV_RE.match(p)
            return int(re.match(r'\d+', m.group(2)).group()) if m else -1
        nums = sorted({para(p) for p in provs if para(p) >= 0})
        gaps = [n for n in range(min(nums), max(nums)+1) if n not in nums] if nums else []
        flag = "⚠ GAPS" if gaps else "ok"
        print(f"  Article {art}: {len(provs)} provisions, "
              f"paras {min(nums) if nums else '-'}–{max(nums) if nums else '-'} [{flag}]")
        if gaps:
            print(f"      missing paragraph numbers: {gaps[:25]}")
            print(f"      ^ check these pages in the PDF — likely a margin-x0 or "
                  f"footnote-filter miss")
    # Body sanity
    empties = [r["provision"] for r in records if len(r["body"]) < 15]
    if empties:
        print(f"  ⚠ {len(empties)} provisions with suspiciously short body: {empties[:15]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="print layout stats to tune CONFIG (run this first)")
    ap.add_argument("--article", type=str, default=None,
                    help="extract only this article number, e.g. 9")
    ap.add_argument("--pdf", type=str, default=None, help="override PDF path")
    ap.add_argument("--out", type=str, default="/mnt/user-data/outputs/rr_provisions.jsonl")
    args = ap.parse_args()

    pdf_path = args.pdf or CONFIG["PDF_PATH"]

    if args.inspect:
        inspect(pdf_path)
        return

    recs = extract(pdf_path, only_article=args.article)
    validate(recs)

    with open(args.out, "w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(recs)} records -> {args.out}")
    print("Spot-check: open the file and verify 3 random provisions against the PDF.")


if __name__ == "__main__":
    main()
