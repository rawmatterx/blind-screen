"""Core logic for blinded human + Claude dual screening.

Standard library only (Python 3.9+). Every stage of a review lives in
<project>/2_Screening/blind/<stage>/ where stage is "ta" (title/abstract)
or "ft" (full text).

Files in a stage directory:
  config.json             criteria text, exclusion codes, stage, created
  records.jsonl           one record per line, fixed ids
  batches/                record-only batch files for Claude sub-agents
  .sealed/staging/        Claude's decisions while it is still screening
  .sealed/claude_decisions.jsonl   merged + sealed Claude decisions (read-only)
  seal.json               SHA-256 of the sealed file, time, model, count
  human_decisions.jsonl   append-only log of the human's keypresses
  lock.json               written at reconcile: hashes of both decision sets
  adjudication.jsonl      append-only log of the human's final decisions
"""

import csv
import datetime as _dt
import hashlib
import json
import os
import re
import stat

DECISIONS = ("INCLUDE", "EXCLUDE", "UNSURE")
FINAL_DECISIONS = ("INCLUDE", "EXCLUDE")

DEFAULT_CODES = {
    "ta": {
        "E1": "Not the target population",
        "E2": "Not the index test / intervention",
        "E3": "Ineligible study design or publication type",
        "E4": "No relevant outcome",
        "E5": "Non-human / in vitro",
        "E6": "Duplicate",
    },
    "ft": {
        "F1": "No extractable outcome data",
        "F2": "No comparative / reference-standard data",
        "F3": "Cannot separate target-population data",
        "F4": "Ineligible design, sample or follow-up",
        "F5": "Overlapping cohort (duplicate patients)",
    },
}


class BlindScreenError(Exception):
    """A rule of the blinded workflow was broken; the message says which."""


# --------------------------------------------------------------------------
# paths and small helpers
# --------------------------------------------------------------------------

def stage_dir(project, stage):
    if stage not in ("ta", "ft"):
        raise BlindScreenError("stage must be 'ta' or 'ft', not %r" % stage)
    return os.path.join(project, "2_Screening", "blind", stage)


def screening_dir(project):
    return os.path.join(project, "2_Screening")


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today():
    return _dt.date.today().isoformat()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                raise BlindScreenError("%s line %d is not valid JSON" % (path, n))
    return out


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def make_readonly(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


# --------------------------------------------------------------------------
# reading search exports
# --------------------------------------------------------------------------

_FIELD_ALIASES = {
    "id": ("record_id", "id", "study_id", "key", "covidence #", "rayyan id"),
    "title": ("title", "ti", "article title", "document title", "primary title"),
    "abstract": ("abstract", "ab", "abstract note", "notes_abstract"),
    "authors": ("authors", "author", "au", "author full names", "author names"),
    "year": ("year", "py", "publication year", "pubyear", "date"),
    "journal": ("journal", "source title", "source", "jo", "t2", "journal/book", "publication title"),
    "doi": ("doi", "do", "di"),
    "pmid": ("pmid", "pubmed id", "pubmed_id", "pm"),
}


def _pick(row, field):
    lower = {k.strip().lower(): v for k, v in row.items() if k is not None}
    for alias in _FIELD_ALIASES[field]:
        v = lower.get(alias)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _parse_delimited(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if path.lower().endswith((".tsv", ".tab")) or sample.count("\t") > sample.count(",") else ","
        return list(csv.DictReader(f, delimiter=delim))


_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")


def _parse_ris(path):
    rows, cur = [], {}
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            m = _RIS_LINE.match(line)
            if not m:
                continue
            tag, val = m.group(1), m.group(2).strip()
            if tag == "ER":
                if cur:
                    rows.append(cur)
                cur = {}
                continue
            if tag in ("AU", "A1"):
                cur["authors"] = (cur.get("authors", "") + "; " + val).strip("; ")
            elif tag in ("TI", "T1") and "title" not in cur:
                cur["title"] = val
            elif tag in ("AB", "N2"):
                cur["abstract"] = (cur.get("abstract", "") + " " + val).strip()
            elif tag in ("PY", "Y1", "DA") and "year" not in cur:
                cur["year"] = val[:4]
            elif tag in ("JO", "T2", "JF") and "journal" not in cur:
                cur["journal"] = val
            elif tag == "DO":
                cur["doi"] = val
            elif tag == "AN" and val.isdigit():
                cur["pmid"] = val
            elif tag == "ID" and "id" not in cur:
                cur["id"] = val
    if cur:
        rows.append(cur)
    return rows


def _parse_medline(path):
    """PubMed 'MEDLINE' / .nbib format."""
    rows, cur, last = [], {}, None
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line.strip():
                if cur:
                    rows.append(cur)
                cur, last = {}, None
                continue
            if line.startswith("      ") and last:
                cur[last] = cur[last] + " " + line.strip()
                continue
            m = re.match(r"^([A-Z]{2,4})\s*- (.*)$", line)
            if not m:
                continue
            tag, val = m.group(1), m.group(2).strip()
            key = {"PMID": "pmid", "TI": "title", "AB": "abstract", "JT": "journal", "DP": "year"}.get(tag)
            if tag == "AU":
                cur["authors"] = (cur.get("authors", "") + "; " + val).strip("; ")
                last = None
            elif tag in ("LID", "AID") and "[doi]" in val:
                cur["doi"] = val.replace("[doi]", "").strip()
                last = None
            elif key:
                cur[key] = val[:4] if key == "year" else val
                last = key
            else:
                last = None
    if cur:
        rows.append(cur)
    return rows


def load_records(path):
    """Read CSV / TSV / RIS / MEDLINE(.nbib/.txt) / JSONL into normalised dicts."""
    low = path.lower()
    if low.endswith((".ris", ".enw")):
        raw = _parse_ris(path)
    elif low.endswith((".nbib", ".medline")) or (low.endswith(".txt") and _looks_medline(path)):
        raw = _parse_medline(path)
    elif low.endswith(".jsonl"):
        raw = read_jsonl(path)
    elif low.endswith((".csv", ".tsv", ".tab", ".txt")):
        raw = _parse_delimited(path)
    else:
        raise BlindScreenError("Unrecognised file type: %s (use CSV, TSV, RIS, NBIB or JSONL)" % path)

    records, seen = [], set()
    for n, row in enumerate(raw, 1):
        rec = {f: _pick(row, f) for f in _FIELD_ALIASES}
        if not rec["title"] and not rec["abstract"]:
            continue
        rid = rec["id"] or ("R%05d" % n)
        rid = re.sub(r"[^A-Za-z0-9_.-]", "_", rid)
        if rid in seen:
            raise BlindScreenError("Duplicate record id %r in %s; ids must be unique" % (rid, path))
        seen.add(rid)
        rec["id"] = rid
        # carry earlier-round columns through (for full-text stage)
        for k in ("round3_decision",):
            v = row.get(k) if isinstance(row, dict) else None
            if v:
                rec[k] = v.strip()
        records.append(rec)
    if not records:
        raise BlindScreenError("No records with a title or abstract found in %s" % path)
    return records


def _looks_medline(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        head = f.read(2000)
    return bool(re.search(r"^PMID- ", head, re.M))


_CODE_LINE = re.compile(r"^\s*[-*]?\s*\**([EF]\d{1,2})\**\s*[:=–—-]\s*(.+?)\s*$")


def parse_codes(text, stage):
    """Pull 'E1: reason' / 'F2 = reason' lines out of a criteria file."""
    prefix = "E" if stage == "ta" else "F"
    codes = {}
    for line in text.splitlines():
        m = _CODE_LINE.match(line)
        if m and m.group(1).startswith(prefix):
            codes[m.group(1)] = m.group(2)
    return codes or dict(DEFAULT_CODES[stage])


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_pdfs(records, pdf_dir):
    """Attach a pdf filename to each record: <id>.pdf, or a file containing its PMID or DOI."""
    files = sorted(f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf"))
    missing = []
    for r in records:
        hit = None
        rid = r["id"].lower()
        pmid = r.get("pmid", "").strip()
        doi = _slug(r.get("doi", ""))
        for f in files:
            stem = os.path.splitext(f)[0]
            low = stem.lower()
            tokens = re.split(r"[^a-z0-9.]+", low)
            if (low == rid or rid in tokens
                    or (pmid and re.search(r"(?<!\d)%s(?!\d)" % re.escape(pmid), stem))
                    or (len(doi) >= 6 and doi in _slug(stem))):
                hit = f
                break
        r["pdf"] = hit
        if not hit:
            missing.append(r["id"])
    return missing


def prepare(project, stage, records_path, criteria_path, pdf_dir=None, allow_missing=False):
    sd = stage_dir(project, stage)
    if os.path.exists(os.path.join(sd, "records.jsonl")):
        raise BlindScreenError(
            "Stage %r is already prepared in %s. Screening ids are fixed once prepared; "
            "use a new project folder for a new search." % (stage, sd))
    with open(criteria_path, encoding="utf-8") as f:
        criteria = f.read()
    records = load_records(records_path)

    if stage == "ft" and any("round3_decision" in r for r in records):
        records = [r for r in records if r.get("round3_decision", "").upper() == "INCLUDE"]
        if not records:
            raise BlindScreenError("No round3_decision = INCLUDE rows to take to full text")

    not_retrieved = []
    if stage == "ft":
        if not pdf_dir:
            raise BlindScreenError("Full-text stage needs --pdf-dir")
        missing = match_pdfs(records, pdf_dir)
        if missing and not allow_missing:
            raise BlindScreenError(
                "%d record(s) have no PDF yet: %s. Add the PDFs (named by record id, PMID or DOI) "
                "or rerun with --allow-missing to record them as 'reports not retrieved'."
                % (len(missing), ", ".join(missing)))
        not_retrieved = [r for r in records if not r.get("pdf")]
        records = [r for r in records if r.get("pdf")]

    os.makedirs(os.path.join(sd, ".sealed", "staging"), exist_ok=True)
    os.makedirs(os.path.join(sd, "batches"), exist_ok=True)
    for r in records:
        r.pop("round3_decision", None)
    write_jsonl(os.path.join(sd, "records.jsonl"), records)
    cfg = {
        "stage": stage,
        "created": now_iso(),
        "source_file": os.path.abspath(records_path),
        "source_sha256": sha256_file(records_path),
        "criteria_file": os.path.abspath(criteria_path),
        "criteria": criteria,
        "codes": parse_codes(criteria, stage),
        "pdf_dir": os.path.abspath(pdf_dir) if pdf_dir else None,
        "n_records": len(records),
        "not_retrieved": [{"id": r["id"], "title": r.get("title", "")} for r in not_retrieved],
    }
    write_json(os.path.join(sd, "config.json"), cfg)
    return cfg


def load_config(project, stage):
    sd = stage_dir(project, stage)
    cfg = read_json(os.path.join(sd, "config.json"))
    if cfg is None:
        raise BlindScreenError("Stage %r is not prepared yet. Run: bs.py prepare --stage %s ..." % (stage, stage))
    return cfg


def load_stage_records(project, stage):
    return read_jsonl(os.path.join(stage_dir(project, stage), "records.jsonl"))


# --------------------------------------------------------------------------
# Claude's side: batches, staging, seal
# --------------------------------------------------------------------------

def make_batches(project, stage, size=50):
    sd = stage_dir(project, stage)
    if is_sealed(project, stage):
        raise BlindScreenError("Claude's decisions are already sealed for this stage.")
    recs = load_stage_records(project, stage)
    bdir = os.path.join(sd, "batches")
    for f in os.listdir(bdir):
        os.remove(os.path.join(bdir, f))
    names = []
    for i in range(0, len(recs), size):
        name = "batch_%03d" % (i // size + 1)
        write_jsonl(os.path.join(bdir, name + ".jsonl"), recs[i:i + size])
        names.append(name)
    return names


def validate_decision(d, codes, final=False):
    allowed = FINAL_DECISIONS if final else DECISIONS
    if d.get("decision") not in allowed:
        raise BlindScreenError("decision must be one of %s, got %r (record %s)"
                               % ("/".join(allowed), d.get("decision"), d.get("id")))
    if d["decision"] == "EXCLUDE":
        if d.get("code") not in codes:
            raise BlindScreenError("EXCLUDE needs an exclusion code from %s (record %s, got %r)"
                                   % (", ".join(sorted(codes)), d.get("id"), d.get("code")))
    else:
        d["code"] = ""


def staging_status(project, stage):
    """Return (decided ids, missing ids, problems) for the staged Claude decisions."""
    sd = stage_dir(project, stage)
    cfg = load_config(project, stage)
    ids = [r["id"] for r in load_stage_records(project, stage)]
    idset = set(ids)
    got, problems = {}, []
    sdir = os.path.join(sd, ".sealed", "staging")
    for f in sorted(os.listdir(sdir)):
        if not f.endswith(".jsonl"):
            continue
        for d in read_jsonl(os.path.join(sdir, f)):
            rid = d.get("id")
            if rid not in idset:
                problems.append("%s: unknown id %r" % (f, rid))
                continue
            try:
                validate_decision(d, cfg["codes"])
            except BlindScreenError as e:
                problems.append("%s: %s" % (f, e))
                continue
            if rid in got:
                problems.append("%s: id %s decided twice" % (f, rid))
            got[rid] = d
    missing = [i for i in ids if i not in got]
    return got, missing, problems


def is_sealed(project, stage):
    return os.path.exists(os.path.join(stage_dir(project, stage), "seal.json"))


def seal(project, stage, model):
    sd = stage_dir(project, stage)
    if is_sealed(project, stage):
        raise BlindScreenError("Already sealed; a seal cannot be redone.")
    got, missing, problems = staging_status(project, stage)
    if problems:
        raise BlindScreenError("Fix these before sealing:\n  " + "\n  ".join(problems))
    if missing:
        raise BlindScreenError("Claude has not screened %d record(s) yet: %s"
                               % (len(missing), ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")))
    if read_jsonl(os.path.join(sd, "human_decisions.jsonl")):
        raise BlindScreenError(
            "Human decisions already exist for this stage. Claude must be sealed before the human "
            "starts, so that Claude provably could not have seen them.")
    ids = [r["id"] for r in load_stage_records(project, stage)]
    rows = []
    for rid in ids:
        d = got[rid]
        rows.append({"id": rid, "decision": d["decision"], "code": d.get("code", ""),
                     "reason": (d.get("reason") or "").strip()})
    path = os.path.join(sd, ".sealed", "claude_decisions.jsonl")
    write_jsonl(path, rows)
    make_readonly(path)
    info = {"sealed_at": now_iso(), "model": model, "n": len(rows),
            "sha256": sha256_file(path), "file": ".sealed/claude_decisions.jsonl"}
    write_json(os.path.join(sd, "seal.json"), info)
    make_readonly(os.path.join(sd, "seal.json"))
    # staging copies are no longer needed and would be a second, unsealed copy
    sdir = os.path.join(sd, ".sealed", "staging")
    for f in os.listdir(sdir):
        os.remove(os.path.join(sdir, f))
    return info


def verify_seal(project, stage):
    sd = stage_dir(project, stage)
    info = read_json(os.path.join(sd, "seal.json"))
    if info is None:
        raise BlindScreenError("Claude's decisions are not sealed yet.")
    actual = sha256_file(os.path.join(sd, info["file"]))
    if actual != info["sha256"]:
        raise BlindScreenError("SEAL BROKEN: Claude's decision file changed after sealing "
                               "(expected %s, found %s). Do not use this stage." % (info["sha256"], actual))
    return info


def load_claude(project, stage):
    """Only for reconcile/adjudication. Never call this before the human has finished."""
    verify_seal(project, stage)
    rows = read_jsonl(os.path.join(stage_dir(project, stage), ".sealed", "claude_decisions.jsonl"))
    return {r["id"]: r for r in rows}


# --------------------------------------------------------------------------
# human side
# --------------------------------------------------------------------------

def latest(log_rows):
    """Last entry per id wins; an 'undo' entry clears the id."""
    out = {}
    for r in log_rows:
        if r.get("decision") == "UNDO":
            out.pop(r["id"], None)
        else:
            out[r["id"]] = r
    return out


def human_path(project, stage):
    return os.path.join(stage_dir(project, stage), "human_decisions.jsonl")


def adjudication_path(project, stage):
    return os.path.join(stage_dir(project, stage), "adjudication.jsonl")


def is_locked(project, stage):
    return os.path.exists(os.path.join(stage_dir(project, stage), "lock.json"))


def record_human(project, stage, rid, decision, code="", note=""):
    if not is_sealed(project, stage):
        raise BlindScreenError("Claude's screening must be sealed before you start, so the two "
                               "reviews are provably independent.")
    if is_locked(project, stage):
        raise BlindScreenError("Your screening for this stage is locked (reconcile has run).")
    cfg = load_config(project, stage)
    ids = {r["id"] for r in load_stage_records(project, stage)}
    if rid not in ids:
        raise BlindScreenError("Unknown record id %r" % rid)
    row = {"id": rid, "decision": decision, "code": code or "", "note": note or "", "ts": now_iso()}
    if decision != "UNDO":
        validate_decision(row, cfg["codes"])
    append_jsonl(human_path(project, stage), row)
    return row


def record_adjudication(project, stage, rid, decision, code="", note=""):
    if not is_locked(project, stage):
        raise BlindScreenError("Run reconcile before adjudicating.")
    if os.path.exists(os.path.join(stage_dir(project, stage), "final.json")):
        raise BlindScreenError("This stage is finalised; adjudications are closed.")
    cfg = load_config(project, stage)
    queue = set(adjudication_queue(project, stage))
    if rid not in queue:
        raise BlindScreenError("Record %s is not in the disagreement queue." % rid)
    row = {"id": rid, "decision": decision, "code": code or "", "note": note or "", "ts": now_iso()}
    if decision != "UNDO":
        validate_decision(row, cfg["codes"], final=True)
    append_jsonl(adjudication_path(project, stage), row)
    return row


# --------------------------------------------------------------------------
# agreement statistics
# --------------------------------------------------------------------------

def cohen_kappa(a, b):
    """Cohen's kappa for two equal-length label lists. Returns None if undefined."""
    if len(a) != len(b) or not a:
        raise ValueError("need two non-empty lists of equal length")
    n = float(len(a))
    labels = sorted(set(a) | set(b))
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    if pe == 1.0:
        return 1.0 if po == 1.0 else None
    return (po - pe) / (1.0 - pe)


def binary(decision):
    """Primary analysis: UNSURE goes forward with INCLUDE (conservative screening)."""
    return "EXCLUDE" if decision == "EXCLUDE" else "INCLUDE"


def tag(h, c):
    if h == c and h in FINAL_DECISIONS:
        return h
    return "MAYBE"


# --------------------------------------------------------------------------
# reconcile and finalise
# --------------------------------------------------------------------------

def progress(project, stage):
    recs = load_stage_records(project, stage)
    done = latest(read_jsonl(human_path(project, stage)))
    return len([r for r in recs if r["id"] in done]), len(recs)


def _tsv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header])


def _reason(d, codes):
    if not d:
        return ""
    if d["decision"] == "EXCLUDE":
        return "%s %s" % (d["code"], codes.get(d["code"], ""))
    return d.get("note") or d.get("reason") or ""


def reconcile(project, stage, date=None):
    date = date or today()
    sd = stage_dir(project, stage)
    cfg = load_config(project, stage)
    if is_locked(project, stage):
        raise BlindScreenError("Already reconciled. Next: serve --adjudicate, then finalize.")
    seal_info = verify_seal(project, stage)
    recs = load_stage_records(project, stage)
    human = latest(read_jsonl(human_path(project, stage)))
    missing = [r["id"] for r in recs if r["id"] not in human]
    if missing:
        raise BlindScreenError("You have not screened %d record(s) yet. Finish before unsealing Claude."
                               % len(missing))
    hpath = human_path(project, stage)
    make_readonly(hpath)
    claude = load_claude(project, stage)

    h3 = [human[r["id"]]["decision"] for r in recs]
    c3 = [claude[r["id"]]["decision"] for r in recs]
    h2 = [binary(x) for x in h3]
    c2 = [binary(x) for x in c3]
    n = len(recs)
    agree2 = sum(1 for x, y in zip(h2, c2) if x == y)
    stats = {
        "stage": stage,
        "n": n,
        "kappa_binary": cohen_kappa(h2, c2),
        "kappa_three_category": cohen_kappa(h3, c3),
        "percent_agreement_binary": round(100.0 * agree2 / n, 1),
        "both_include": sum(1 for x, y in zip(h3, c3) if x == y == "INCLUDE"),
        "both_exclude": sum(1 for x, y in zip(h3, c3) if x == y == "EXCLUDE"),
        "human_only_include": sum(1 for x, y in zip(h2, c2) if x == "INCLUDE" and y == "EXCLUDE"),
        "claude_only_include": sum(1 for x, y in zip(h2, c2) if x == "EXCLUDE" and y == "INCLUDE"),
        "either_unsure": sum(1 for x, y in zip(h3, c3) if "UNSURE" in (x, y)),
        "note": "Binary kappa treats UNSURE as INCLUDE (records go forward when either reviewer is unsure).",
    }

    rows, maybe = [], []
    for r in recs:
        hd, cd = human[r["id"]], claude[r["id"]]
        t = tag(hd["decision"], cd["decision"])
        if t == "MAYBE":
            maybe.append(r["id"])
        rows.append({
            "record_id": r["id"], "title": r.get("title", ""), "year": r.get("year", ""),
            "doi": r.get("doi", ""), "pmid": r.get("pmid", ""),
            "round1_decision": hd["decision"], "round1_reason": _reason(hd, cfg["codes"]),
            "round2_decision": cd["decision"], "round2_reason": _reason(cd, cfg["codes"]),
            "round2_tag": t,
        })
    stats["maybe"] = len(maybe)

    out = screening_dir(project)
    prefix = "" if stage == "ta" else "fulltext_"
    r1 = os.path.join(out, "%sround1_%s.tsv" % (prefix, date))
    r2 = os.path.join(out, "%sround2_%s.tsv" % (prefix, date))
    _tsv(r1, ["record_id", "title", "year", "doi", "pmid", "round1_decision", "round1_reason"], rows)
    _tsv(r2, ["record_id", "title", "year", "doi", "pmid", "round1_decision", "round1_reason",
              "round2_decision", "round2_reason", "round2_tag"], rows)
    write_json(os.path.join(out, "%skappa_%s.json" % (prefix, date)), stats)

    lock = {"locked_at": now_iso(), "human_sha256": sha256_file(hpath),
            "claude_sha256": seal_info["sha256"], "maybe_ids": maybe,
            "round1_file": os.path.relpath(r1, project), "round2_file": os.path.relpath(r2, project),
            "date": date}
    write_json(os.path.join(sd, "lock.json"), lock)
    make_readonly(os.path.join(sd, "lock.json"))
    return stats, lock


def adjudication_queue(project, stage):
    """Records needing a final human decision, with both reviewers' calls (only after lock)."""
    if not is_locked(project, stage):
        raise BlindScreenError("Not reconciled yet.")
    lock = read_json(os.path.join(stage_dir(project, stage), "lock.json"))
    return lock["maybe_ids"]


def finalize(project, stage, model_label=None):
    sd = stage_dir(project, stage)
    cfg = load_config(project, stage)
    if not is_locked(project, stage):
        raise BlindScreenError("Run reconcile first.")
    if os.path.exists(os.path.join(sd, "final.json")):
        raise BlindScreenError("Already finalised.")
    lock = read_json(os.path.join(sd, "lock.json"))
    verify_seal(project, stage)
    if sha256_file(human_path(project, stage)) != lock["human_sha256"]:
        raise BlindScreenError("Your screening log changed after it was locked. Do not use this stage.")
    recs = load_stage_records(project, stage)
    human = latest(read_jsonl(human_path(project, stage)))
    claude = load_claude(project, stage)
    adj = latest(read_jsonl(adjudication_path(project, stage)))
    pending = [i for i in lock["maybe_ids"] if i not in adj]
    if pending:
        raise BlindScreenError("%d disagreement(s) still need your decision." % len(pending))

    rows, excl_codes = [], {}
    for r in recs:
        hd, cd = human[r["id"]], claude[r["id"]]
        t = tag(hd["decision"], cd["decision"])
        if t == "MAYBE":
            fd = adj[r["id"]]
            final, reason = fd["decision"], _reason(fd, cfg["codes"])
            by = "adjudicated by human reviewer"
            code = fd["code"]
        else:
            final = t
            src = hd
            reason = _reason(src, cfg["codes"])
            code = src["code"] if final == "EXCLUDE" else ""
            by = "agreement"
        if final == "EXCLUDE":
            excl_codes[code] = excl_codes.get(code, 0) + 1
        rows.append({
            "record_id": r["id"], "title": r.get("title", ""), "year": r.get("year", ""),
            "doi": r.get("doi", ""), "pmid": r.get("pmid", ""),
            "round1_decision": hd["decision"], "round1_reason": _reason(hd, cfg["codes"]),
            "round2_decision": cd["decision"], "round2_reason": _reason(cd, cfg["codes"]),
            "round2_tag": t, "round3_decision": final,
            "round3_reason": reason if t == "MAYBE" else "", "resolved_by": by,
            "exclusion_code": code,
        })

    out = screening_dir(project)
    prefix = "" if stage == "ta" else "fulltext_"
    date = lock["date"]
    r3 = os.path.join(out, "%sround3_%s.tsv" % (prefix, date))
    header = ["record_id", "title", "year", "doi", "pmid", "round1_decision", "round1_reason",
              "round2_decision", "round2_reason", "round2_tag", "round3_decision", "round3_reason",
              "resolved_by", "exclusion_code"]
    _tsv(r3, header, rows)
    if stage == "ft":
        # medsci-skills 3f reads fulltext_screening.tsv (Include/Exclude per id)
        _tsv(os.path.join(out, "fulltext_screening.tsv"),
             ["record_id", "decision", "exclusion_code", "reason"],
             [{"record_id": x["record_id"], "decision": x["round3_decision"].capitalize(),
               "exclusion_code": x["exclusion_code"],
               "reason": cfg["codes"].get(x["exclusion_code"], "")} for x in rows])

    stats = read_json(os.path.join(out, "%skappa_%s.json" % (prefix, date)))
    included = sum(1 for x in rows if x["round3_decision"] == "INCLUDE")
    prisma = {
        "stage": stage,
        "records_screened" if stage == "ta" else "reports_assessed": len(rows),
        "excluded": len(rows) - included,
        "excluded_by_code": {k: {"n": v, "reason": cfg["codes"].get(k, "")} for k, v in sorted(excl_codes.items())},
        "included": included,
    }
    if stage == "ft":
        prisma["reports_not_retrieved"] = len(cfg.get("not_retrieved", []))
        prisma["reports_sought"] = len(rows) + prisma["reports_not_retrieved"]
    write_json(os.path.join(out, "%sprisma_counts_%s.json" % (prefix, date)), prisma)

    seal_info = read_json(os.path.join(sd, "seal.json"))
    methods = methods_text(stage, stats, prisma, seal_info, model_label)
    mpath = os.path.join(out, "%smethods_blind_screening_%s.md" % (prefix, date))
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(methods)

    final = {"finalized_at": now_iso(), "round3_file": os.path.relpath(r3, project),
             "round3_sha256": sha256_file(r3), "prisma": prisma}
    write_json(os.path.join(sd, "final.json"), final)
    make_readonly(os.path.join(sd, "final.json"))
    make_readonly(adjudication_path(project, stage)) if os.path.exists(adjudication_path(project, stage)) else None
    return final, prisma, mpath


def _fmt_k(k):
    return "not estimable" if k is None else "%.2f" % k


def methods_text(stage, stats, prisma, seal_info, model_label=None):
    model = model_label or seal_info.get("model", "Claude")
    level = "titles and abstracts" if stage == "ta" else "full-text reports"
    n = stats["n"]
    return (
        "### Study selection ({level})\n\n"
        "Two reviewers screened all {n} {level} independently against the prespecified eligibility "
        "criteria: a human reviewer and a large language model ({model}, Anthropic) used as an "
        "independent second reviewer. The model was given only the protocol's eligibility criteria and "
        "exclusion codes and each record's bibliographic details{ft}; it had no access to the human "
        "reviewer's decisions. Its decisions (include, exclude with a prespecified reason code, or "
        "unsure, with a one-sentence justification) were written to a file that was cryptographically "
        "sealed (SHA-256) on {sealed} before the human reviewer began screening. The human reviewer "
        "screened in a purpose-built interface that withheld the model's decisions, and the human "
        "decision log was locked and hashed before the model's decisions were unsealed.\n\n"
        "Agreement between reviewers was {pa}% (Cohen's κ = {kb} for include versus exclude, with "
        "'unsure' counted as include; three-category κ = {k3}). All {m} records with any disagreement "
        "or an 'unsure' rating from either reviewer were resolved by the human reviewer, who saw both "
        "reviewers' decisions and reasons; the model did not make any final inclusion decision. "
        "{inc} of {n} records were included at this stage.\n\n"
        "_Sealed file SHA-256: {sha}._\n"
    ).format(level=level, n=n, model=model,
             ft=" and the full text of each report" if stage == "ft" else " (title, abstract, authors, year, journal)",
             sealed=seal_info["sealed_at"][:10], pa=stats["percent_agreement_binary"],
             kb=_fmt_k(stats["kappa_binary"]), k3=_fmt_k(stats["kappa_three_category"]),
             m=stats["maybe"], inc=prisma["included"], sha=seal_info["sha256"])
