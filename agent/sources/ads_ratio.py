"""ADS ratios from the cover pages of 20-F / 40-F filings -> panel.ads_ratio (S53a, 2026-10-14).

A 20-F filer's XBRL share counts are ordinary shares; the price in bars is per listed unit, which for most
of them is an American depositary share (ADS). BABA: one ADS = 8 ordinary shares, so ordinary shares x ADS
price overstates the market cap 8x. TCOM until 2021: one ADS = 1/8 of a share, understated 8x (ranked 7th
of the long book's universe in 2020-04 on a value score of +3.7). The ratio is not in companyfacts; it is
on the cover page of every 20-F, in the table of securities registered under Section 12(b):
  "American Depositary Shares, each representing eight Ordinary Shares"
  "American depositary shares (one American depositary share representing five Class A ordinary shares)"
  "American depositary shares (each representing one ordinary share, par value US$0.00125 per share)"

ratio = ordinary shares per listed unit. A 12(b) table without any depositary share (common shares listed
directly: DAC, AER, every 40-F so far) is ratio 1. Parsing is deterministic (regular expressions, no LLM);
a cover with an ADS and no ratio, or with ratios that disagree, is left for agent/ads_ratio.yaml, the
hand-checked table, which wins over the parse.

Two steps, so that the panel's write lock is held for a second, never during downloads (S53a; the 13:25
and 16:10 jobs write panel.db):
  fetch   download each filing's primary document up to the end of its cover page (streamed, the
          inline-XBRL header can be 300 KB), parse, append to CACHE (jsonl, resumable)
  commit  CACHE + agent/ads_ratio.yaml -> panel.ads_ratio, one transaction

Usage: python -m agent.sources.ads_ratio fetch [--limit N] [--cik C ...]
       python -m agent.sources.ads_ratio commit
       python -m agent.sources.ads_ratio show [--status ads_no_ratio]
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import time
import urllib.request
from fractions import Fraction
from pathlib import Path

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

FORMS = ("20-F", "40-F", "20-F/A", "40-F/A")
CACHE = Path.home() / ".hedge-fund" / "agent" / "ads_ratio_cache.jsonl"
MANUAL = Path(__file__).resolve().parents[1] / "ads_ratio.yaml"
SUBMISSIONS = "https://data.sec.gov/submissions/{name}"
DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
PAUSE = 0.4                       # per worker before each request; 4 workers stay well inside SEC's 10/second
MAX_BYTES = 6_000_000             # stop reading a document here even if the cover's end was not seen
FULL_MAX_BYTES = 40_000_000       # whole-document pass for covers with a depositary share and no ratio
COVER_BYTES = 150_000             # raw bytes read past the well-known-seasoned-issuer question (cover footnotes)
COVER_TAIL = 12_000               # characters of text kept past it
DDL = """CREATE TABLE IF NOT EXISTS ads_ratio (
    cik INT, accn VARCHAR, form VARCHAR, filed DATE, ratio DOUBLE, status VARCHAR, cover_text VARCHAR,
    doc VARCHAR, source VARCHAR, fetched_at TIMESTAMP, PRIMARY KEY (cik, accn))"""

# ---------------------------------------------------------------- numbers in words
_UNITS = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_DENOM = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fourth": 4, "fourths": 4,
          "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6, "eighth": 8, "eighths": 8, "tenth": 10, "tenths": 10,
          "twentieth": 20, "twentieths": 20, "fortieth": 40, "fiftieth": 50, "hundredth": 100, "hundredths": 100}
_WORD = "|".join(sorted([*_UNITS, *_TENS, *_SCALES], key=len, reverse=True))
# a cardinal: digits (1,000 / 0.5 / 1/2) or number words (twenty-five, one hundred), optionally "(8)"
NUM = (rf"(?:\d[\d,]*(?:\.\d+)?(?:\s*/\s*\d+)?|(?:(?:{_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_WORD}))*))"
       r"(?:\s*\(\s*[\d.,/]+\s*\))?")
FRAC = rf"(?:(?:{_WORD}|\d+)[\s-]+(?:{'|'.join(_DENOM)})(?:\s+of\s+(?:one|an?|1))?)"
SHARE = (r"(?:(?:class\s+[a-z]\s+|series\s+[a-z]\s+|registered\s+|bearer\s+|ordinary\s+|common\s+|equity\s+|"
         r"restricted\s+voting\s+|subordinate\s+voting\s+|non-?voting\s+|voting\s+|h\s+|a\s+|b\s+)*"
         r"(?:ordinary|common|equity|preferred|class\s+[a-z]|registered)?\s*(?:share|stock|unit)s?)")
ADS = r"(?:american\s+depositary\s+(?:shares?|receipts?)|american\s+depository\s+shares?|\bads'?s?\b|\badrs?\b)"


def _words(s: str) -> float | None:
    s = s.lower().replace("-", " ").replace(" and ", " ")
    total, cur = 0, 0
    for w in s.split():
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w in _SCALES:
            cur = max(cur, 1) * _SCALES[w]
            if _SCALES[w] >= 1000:
                total, cur = total + cur, 0
        else:
            return None
    return float(total + cur)


def number(s: str) -> float | None:
    """'eight' -> 8, 'twenty-five' -> 25, '1,000' -> 1000, '0.5' -> 0.5, '1/2' -> 0.5, 'eight (8)' -> 8,
    'one-half of one' -> 0.5, 'two-thirds of one' -> 0.667. None if not a number."""
    s = re.sub(r"\(\s*[\d.,/]+\s*\)", "", s).strip().lower()
    m = re.fullmatch(rf"((?:{_WORD})|\d+)[\s-]+({'|'.join(_DENOM)})(?:\s+of\s+(?:one|an?|1))?", s)
    if m:
        top = float(m.group(1)) if m.group(1).isdigit() else _words(m.group(1))
        return None if top is None else top / _DENOM[m.group(2)]
    if re.fullmatch(r"\d+\s*/\s*\d+", s):
        a, b = (int(x) for x in s.split("/"))
        return a / b if b else None
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", s):
        return float(s.replace(",", ""))
    return _words(s)


# ---------------------------------------------------------------- the cover page
def _plain(raw: str) -> str:
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
    return text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")


def cover_text(raw: str) -> str:
    """The cover page as plain text: from the end of the inline-XBRL header (hidden facts) to COVER_TAIL
    characters past the well-known-seasoned-issuer question, which every 20-F / 40-F cover asks after the
    12(b)/(g)/15(d) tables. The tail keeps the footnotes at the bottom of the cover ("* Not for trading,
    but only in connection with the listing of American Depositary Shares ..."; TSM states no ratio there)."""
    end_hdr = raw.find("/ix:header")
    text = _plain(raw[end_hdr:] if end_hdr >= 0 else raw)
    k = re.search(r"well[- ]known\s+seasoned\s+issuer", text, re.I)
    if not k:
        return text[:60_000]
    # stop at the table of contents: its "Item 12.D American Depositary Shares" and the definitions after it
    # ("ADS ... each of which represented 360 ordinary shares", MFH's ADR program cancelled in 2023) are not
    # the cover's statement of what is listed today
    toc = re.search(r"table\s+of\s+contents", text[k.end():k.end() + COVER_TAIL], re.I)
    return text[: k.end() + (toc.start() if toc else COVER_TAIL)]


def section_12b(cover: str) -> str:
    """The 12(b) table: from 'pursuant to Section 12(b) of the Act' to the 12(g) heading (or 3,000 chars)."""
    m = re.search(r"pursuant\s+to\s+section\s+12\s*\(\s*b\s*\)\s+of\s+the\s+act", cover, re.I)
    if not m:
        return ""
    rest = cover[m.end():]
    g = re.search(r"pursuant\s+to\s+section\s+12\s*\(\s*g\s*\)", rest, re.I)
    return rest[: g.start() if g else 3000]


PATTERNS = [
    # "each (ADS) representing / represents (N) ... shares"; "each of which represents"
    (re.compile(rf"\beach(?:\s+{ADS})?(?:\s+of\s+which)?\s*,?\s+(?:representing|represents|represent)\s+(?:the\s+right\s+to\s+receive\s+)?({FRAC}|{NUM})\s+(?:of\s+(?:our|the\s+registrant's|its)\s+)?{SHARE}", re.I),
     lambda m: number(m.group(1))),
    # "one (1) ADS representing / represents N shares"; "N ADSs representing M shares"
    (re.compile(rf"({NUM})\s+{ADS}\s*,?\s+(?:representing|represents|represent|each\s+representing)\s+(?:the\s+right\s+to\s+receive\s+)?({FRAC}|{NUM})\s+(?:of\s+(?:our|the\s+registrant's)\s+)?{SHARE}", re.I),
     lambda m: (lambda a, b: b / a if a and b is not None else None)(number(m.group(1)), number(m.group(2)))),
    # "American depositary shares, three of which represent two Class A ordinary shares"
    (re.compile(rf"{ADS}\s*(?:\([^)]{{0,20}}\))?\s*,?\s+({NUM})\s+of\s+which\s+(?:representing|represents|represent)\s+({FRAC}|{NUM})\s+{SHARE}", re.I),
     lambda m: (lambda a, b: b / a if a and b is not None else None)(number(m.group(1)), number(m.group(2)))),
    # "each representing 0.1 of a common share"
    (re.compile(rf"\beach(?:\s+{ADS})?\s*,?\s+(?:representing|represents|represent)\s+(\d*\.\d+|{FRAC})\s+of\s+(?:a|an|one)\s+{SHARE}", re.I),
     lambda m: number(m.group(1))),
    # "every N ADSs represent M shares"
    (re.compile(rf"every\s+({NUM})\s+{ADS}\s*,?\s+(?:representing|represents|represent)\s+({FRAC}|{NUM})\s+{SHARE}", re.I),
     lambda m: (lambda a, b: b / a if a and b is not None else None)(number(m.group(1)), number(m.group(2)))),
    # "N shares per ADS" / "N ordinary shares for each ADS"
    (re.compile(rf"({FRAC}|{NUM})\s+{SHARE}\s+(?:per|for\s+each|to\s+each|=)\s+(?:one\s+)?{ADS}", re.I),
     lambda m: number(m.group(1))),
    # "ADS ratio of 1:8" / "ADS to share ratio 1 : 8"
    (re.compile(rf"{ADS}[^.;]{{0,30}}ratio\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*(?::|to)\s*(\d+(?:\.\d+)?)", re.I),
     lambda m: float(m.group(2)) / float(m.group(1)) if float(m.group(1)) else None),
]


def ratios_in(text: str) -> list[tuple[float, str]]:
    """Every ADS ratio stated in text, with the matched phrase."""
    out = []
    for rx, f in PATTERNS:
        for m in rx.finditer(text):
            try:
                v = f(m)
            except Exception:
                v = None
            if v and 1e-6 < v < 1e6:
                out.append((round(v, 6), m.group(0)[:200]))
    return out


def parse(raw: str) -> dict:
    """status, ratio, cover_text. Statuses: parsed_12b / parsed_cover (one ratio found), no_ads (no depositary
    share anywhere on the cover: ratio 1), ads_no_ratio (a depositary share and no ratio on the cover: the
    caller reads the whole document, parse_doc), ambiguous (ratios that disagree), no_cover."""
    cov = cover_text(raw)
    if not re.search(r"12\s*\(\s*b\s*\)|exchange\s+on\s+which\s+registered", cov, re.I):
        return {"status": "no_cover", "ratio": None, "cover_text": cov[:1500]}
    sec = section_12b(cov)
    where = sec or cov[:8000]
    # listed as depositary shares: named in the 12(b) table, or the table's shares carry the footnote "not for
    # trading, but only in connection with the listing of American Depositary Shares" (TSM). An ADS named
    # anywhere else on the cover (a program terminated years ago, a definition) does not make it one.
    footnote = re.search(rf"not\s+for\s+trading[^.]{{0,250}}?{ADS}", cov, re.I)
    if re.search(ADS, where, re.I) is None and footnote is None:
        return {"status": "no_ads", "ratio": 1.0, "cover_text": where[:1500]}
    for scope, text in (("12b", where), ("cover", cov)):
        found = ratios_in(text)
        vals = sorted({v for v, _ in found})
        if len(vals) == 1:
            return {"status": f"parsed_{scope}", "ratio": vals[0], "cover_text": (found[0][1] + " || " + where[:1200])[:1600]}
        if len(vals) > 1:
            return {"status": "ambiguous", "ratio": None,
                    "cover_text": (" | ".join(p for _, p in found) + " || " + where[:1000])[:2000]}
    return {"status": "ads_no_ratio", "ratio": None, "cover_text": where[:1500]}


def parse_doc(raw: str) -> dict:
    """For ads_no_ratio covers: the ratios stated anywhere in the document (Item 9 "Offer and Listing
    Details", Item 12.D "American Depositary Shares"). One value: parsed_doc; several: ambiguous (a ratio
    change described in the text, a second depositary program), left for agent/ads_ratio.yaml."""
    end_hdr = raw.find("/ix:header")                  # the hidden header restates the cover's own title
    text = _plain(raw[end_hdr:] if end_hdr >= 0 else raw)
    found = ratios_in(text)
    vals = sorted({v for v, _ in found})
    if len(vals) == 1:
        return {"status": "parsed_doc", "ratio": vals[0], "cover_text": " | ".join(p for _, p in found[:3])[:1600]}
    if len(vals) > 1:
        cnt = pd.Series([v for v, _ in found]).value_counts()
        return {"status": "ambiguous", "ratio": None,
                "cover_text": ("counts " + json.dumps({str(k): int(n) for k, n in cnt.items()}) + " || "
                               + " | ".join(p for _, p in found[:8]))[:2000]}
    return {"status": "ads_no_ratio", "ratio": None, "cover_text": ""}


# ---------------------------------------------------------------- download
def _get_json(url: str, ua: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def primary_docs(cik: int, ua: str) -> dict[str, tuple[str, str]]:
    """accession -> (form, primary document) from the submissions API, older pages included."""
    out: dict[str, tuple[str, str]] = {}
    d = _get_json(SUBMISSIONS.format(name=f"CIK{cik:010d}.json"), ua)
    pages = [d["filings"]["recent"]]
    for f in d["filings"].get("files", []):
        time.sleep(PAUSE)
        pages.append(_get_json(SUBMISSIONS.format(name=f["name"]), ua))
    for p in pages:
        for acc, form, doc in zip(p["accessionNumber"], p["form"], p["primaryDocument"]):
            out[acc] = (form, doc)
    return out


def read_doc(url: str, ua: str, full: bool = False) -> str:
    """The document from its start through its cover page (full=False) or all of it. SEC ignores Range,
    so the response is streamed and closed COVER_BYTES after the well-known-seasoned-issuer question
    (outside the inline-XBRL header, which never asks it), or at MAX_BYTES."""
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    buf = bytearray()
    stop_at = None
    with urllib.request.urlopen(req, timeout=120) as r:
        while len(buf) < (FULL_MAX_BYTES if full else MAX_BYTES):
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
            if full:
                continue
            if stop_at is None:
                text = buf.decode("utf-8", "replace")
                hdr = text.find("/ix:header")
                start = hdr if hdr >= 0 else (len(text) if "ix:header" in text[:20000] else 0)
                k = re.search(r"well[- ]known\s+seasoned\s+issuer", text[start:], re.I)
                if k:
                    stop_at = len(text[: start + k.end()].encode("utf-8", "replace")) + COVER_BYTES
            if stop_at is not None and len(buf) >= stop_at:
                break
    return buf.decode("utf-8", "replace")


def targets(store: PanelStore, ciks: list[int] | None = None) -> pd.DataFrame:
    """Every 20-F/40-F filing in xbrl_facts of a company with a ticker in bars; companies with financials
    the long book can score come first."""
    q = f"""
        WITH f AS (SELECT cik, accn, any_value(form) AS form, min(filed) AS filed FROM xbrl_facts
                   WHERE form IN ({', '.join(repr(x) for x in FORMS)}) GROUP BY cik, accn),
             t AS (SELECT cik, max(CASE WHEN ni_ttm IS NOT NULL OR equity IS NOT NULL THEN 1 ELSE 0 END) AS fin
                   FROM fundamentals_pit WHERE ticker IN (SELECT DISTINCT ticker FROM bars) GROUP BY cik)
        SELECT f.*, t.fin FROM f JOIN t USING (cik) ORDER BY t.fin DESC, f.cik, f.filed"""
    df = store.con.execute(q).df()
    return df[df["cik"].isin(ciks)] if ciks else df


def done_keys(redo: tuple[str, ...] = ()) -> set[tuple[int, str]]:
    """Filings whose latest record is final: not an error, and not one of the `redo` statuses."""
    if not CACHE.exists():
        return set()
    last: dict[tuple[int, str], str] = {}
    for line in CACHE.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        last[(int(r["cik"]), r["accn"])] = r.get("status", "")
    return {k for k, st in last.items() if st != "error" and st not in redo}


def _one_company(cik: int, rows: list, ua: str) -> list[dict]:
    """Records for one company's filings (one submissions lookup, then each filing's cover)."""
    out = []
    try:
        time.sleep(PAUSE)
        docs = primary_docs(cik, ua)
    except Exception as exc:
        docs, err = None, str(exc)[:300]
    for r in rows:
        rec = {"cik": cik, "accn": r.accn, "form": r.form, "filed": str(pd.Timestamp(r.filed).date()),
               "fetched_at": pd.Timestamp.now().isoformat(timespec="seconds")}
        try:
            if docs is None:
                raise RuntimeError(f"submissions: {err}")
            _, doc = docs.get(r.accn, (None, None))
            if not doc:
                rec.update(status="no_doc", ratio=None, cover_text="", doc="")
            else:
                url = DOC.format(cik=cik, acc=r.accn.replace("-", ""), doc=doc)
                time.sleep(PAUSE)
                got = parse(read_doc(url, ua))
                if got["status"] == "ads_no_ratio":
                    time.sleep(PAUSE)
                    whole = parse_doc(read_doc(url, ua, full=True))
                    if whole["status"] != "ads_no_ratio":
                        whole["cover_text"] = (whole["cover_text"] + " || cover: " + got["cover_text"][:400])[:2000]
                        got = whole
                rec.update(got, doc=url)
        except Exception as exc:
            rec.update(status="error", ratio=None, cover_text=str(exc)[:300], doc="")
        out.append(rec)
    return out


def fetch(limit: int | None = None, ciks: list[int] | None = None, workers: int = 4, redo: tuple[str, ...] = ()) -> dict:
    """Companies in parallel (each worker pauses PAUSE before every request, so `workers` of them stay
    under SEC's 10 requests/second); one writer appends each company's records to CACHE as it completes."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ua = user_agent()
    store = PanelStore(read_only=True)
    try:
        todo = targets(store, ciks)
    finally:
        store.close()
    seen = done_keys(redo)
    todo = todo[[(int(c), a) not in seen for c, a in zip(todo["cik"], todo["accn"])]]
    if limit:
        todo = todo.head(limit)
    stats: dict[str, int] = {"todo": len(todo)}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    groups = [(int(c), list(g.itertuples(index=False))) for c, g in todo.groupby("cik", sort=False)]
    n = 0
    with CACHE.open("a") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one_company, c, rows, ua) for c, rows in groups]
        for fut in as_completed(futs):
            for rec in fut.result():
                out.write(json.dumps(rec) + "\n")
                stats[rec["status"]] = stats.get(rec["status"], 0) + 1
                n += 1
                if n % 200 == 0:
                    print(f"{n}/{len(todo)} {stats}", flush=True)
            out.flush()
    print(stats, flush=True)
    return stats


# ---------------------------------------------------------------- the table
def manual() -> pd.DataFrame:
    """agent/ads_ratio.yaml: hand-checked rows {cik, accn, ratio, source, checked}; they win over the parse."""
    cols = ["cik", "accn", "ratio", "source", "checked"]
    if not MANUAL.exists():
        return pd.DataFrame(columns=cols)
    import yaml
    rows = yaml.safe_load(MANUAL.read_text()) or []
    return pd.DataFrame(rows, columns=cols)


def table() -> pd.DataFrame:
    """CACHE (latest record per filing) with agent/ads_ratio.yaml applied."""
    recs = [json.loads(x) for x in CACHE.read_text().splitlines()] if CACHE.exists() else []
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df = df.drop_duplicates(["cik", "accn"], keep="last").assign(source="cover_parse")
    man = manual()
    if len(man):
        man = man.assign(cik=man["cik"].astype(int))
        df = df.merge(man[["cik", "accn", "ratio", "source"]].rename(columns={"ratio": "m_ratio", "source": "m_src"}),
                      on=["cik", "accn"], how="left")
        hit = df["m_ratio"].notna()
        df.loc[hit, "ratio"] = df.loc[hit, "m_ratio"]
        df.loc[hit, "status"] = "manual"
        df.loc[hit, "source"] = df.loc[hit, "m_src"]
        df = df.drop(columns=["m_ratio", "m_src"])
    return df


def commit(path: str | Path | None = None) -> int:
    """CACHE + agent/ads_ratio.yaml -> panel.ads_ratio (replaced whole, one transaction). path: another panel
    (a copy for a restatement); default the live one."""
    df = table()
    if df.empty:
        print("nothing to commit")
        return 0
    df = df[df["status"] != "error"]
    cols = ["cik", "accn", "form", "filed", "ratio", "status", "cover_text", "doc", "source", "fetched_at"]
    df = df[cols].assign(filed=pd.to_datetime(df["filed"]).dt.date, fetched_at=pd.to_datetime(df["fetched_at"]))
    store = PanelStore(path) if path else PanelStore()
    try:
        store.con.execute(DDL)
        store.con.register("_ads", df)
        store.con.execute("BEGIN")
        store.con.execute("DELETE FROM ads_ratio")
        store.con.execute(f"INSERT INTO ads_ratio SELECT {', '.join(cols)} FROM _ads")
        store.con.execute("COMMIT")
        store.con.unregister("_ads")
    finally:
        store.con.close()
    print(f"ads_ratio: {len(df)} rows, {df['status'].value_counts().to_dict()}")
    return len(df)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "commit", "show"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cik", type=int, nargs="*")
    ap.add_argument("--status")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--panel", help="commit into this panel.db instead of the live one")
    ap.add_argument("--redo", nargs="*", default=[], help="fetch again the filings whose latest status is one of these")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch(a.limit, a.cik, a.workers, tuple(a.redo))
    elif a.cmd == "commit":
        commit(a.panel)
    else:
        df = table()
        if a.status is not None:
            df = df[df["status"] == a.status]
        with pd.option_context("display.width", 250, "display.max_colwidth", 160):
            print(df[["cik", "accn", "form", "filed", "status", "ratio", "cover_text"]].to_string())


if __name__ == "__main__":
    main()
