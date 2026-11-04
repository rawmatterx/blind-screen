import csv
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import bs_core as bs  # noqa: E402

CRITERIA = """# Eligibility
Include: PSMA PET in biochemical recurrence of prostate cancer.

Exclusion codes
- E1: Not prostate cancer
- E2: Not PSMA PET
- E3: Review, editorial or case report
"""


def write_csv(path, n):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Title", "Abstract", "Authors", "Year", "DOI", "PMID"])
        for i in range(n):
            w.writerow(["Study %d" % i, "Abstract %d" % i, "Doe J", "2020", "10.1/x%d" % i, str(1000 + i)])


class Base(unittest.TestCase):
    def setUp(self):
        self.p = tempfile.mkdtemp()
        self.crit = os.path.join(self.p, "criteria.md")
        with open(self.crit, "w") as f:
            f.write(CRITERIA)
        self.src = os.path.join(self.p, "records.csv")
        write_csv(self.src, 6)

    def tearDown(self):
        for root, dirs, files in os.walk(self.p):
            for x in files + dirs:
                os.chmod(os.path.join(root, x), stat.S_IRWXU)
        shutil.rmtree(self.p)

    def stage_claude(self, decisions):
        sd = bs.stage_dir(self.p, "ta")
        rows = [{"id": "R%05d" % (i + 1), "decision": d, "code": "E1" if d == "EXCLUDE" else "", "reason": "r"}
                for i, d in enumerate(decisions)]
        bs.write_jsonl(os.path.join(sd, ".sealed", "staging", "batch_001.jsonl"), rows)


class TestKappa(unittest.TestCase):
    def test_perfect(self):
        self.assertEqual(bs.cohen_kappa(["I", "E", "I"], ["I", "E", "I"]), 1.0)

    def test_known_value(self):
        # classic 2x2: a=20 both yes, b=5, c=10, d=15 -> po=.7, pe=.5 -> k=.4
        a = ["Y"] * 25 + ["N"] * 25
        b = ["Y"] * 20 + ["N"] * 5 + ["Y"] * 10 + ["N"] * 15
        self.assertAlmostEqual(bs.cohen_kappa(a, b), 0.4)

    def test_all_same_label_undefined_disagreement(self):
        self.assertEqual(bs.cohen_kappa(["E", "E"], ["E", "E"]), 1.0)

    def test_tag(self):
        self.assertEqual(bs.tag("INCLUDE", "INCLUDE"), "INCLUDE")
        self.assertEqual(bs.tag("EXCLUDE", "EXCLUDE"), "EXCLUDE")
        self.assertEqual(bs.tag("UNSURE", "UNSURE"), "MAYBE")
        self.assertEqual(bs.tag("INCLUDE", "EXCLUDE"), "MAYBE")


class TestParsing(Base):
    def test_codes(self):
        codes = bs.parse_codes(CRITERIA, "ta")
        self.assertEqual(codes, {"E1": "Not prostate cancer", "E2": "Not PSMA PET",
                                 "E3": "Review, editorial or case report"})

    def test_default_codes(self):
        self.assertIn("F1", bs.parse_codes("no codes here", "ft"))

    def test_csv(self):
        recs = bs.load_records(self.src)
        self.assertEqual(len(recs), 6)
        self.assertEqual(recs[0]["id"], "R00001")
        self.assertEqual(recs[0]["pmid"], "1000")

    def test_ris(self):
        ris = os.path.join(self.p, "x.ris")
        with open(ris, "w") as f:
            f.write("TY  - JOUR\nAU  - Smith A\nAU  - Lee B\nTI  - A title\nAB  - An abstract\n"
                    "PY  - 2021\nDO  - 10.1/abc\nER  - \nTY  - JOUR\nTI  - Second\nER  - \n")
        recs = bs.load_records(ris)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["authors"], "Smith A; Lee B")
        self.assertEqual(recs[0]["doi"], "10.1/abc")

    def test_medline(self):
        nb = os.path.join(self.p, "x.nbib")
        with open(nb, "w") as f:
            f.write("PMID- 123\nTI  - Long title\n      continued\nAB  - Abs\nAU  - Doe A\n"
                    "DP  - 2019 Jan\nLID - 10.9/z [doi]\n\nPMID- 124\nTI  - Two\n")
        recs = bs.load_records(nb)
        self.assertEqual(recs[0]["title"], "Long title continued")
        self.assertEqual(recs[0]["year"], "2019")
        self.assertEqual(recs[0]["doi"], "10.9/z")
        self.assertEqual(recs[1]["pmid"], "124")

    def test_duplicate_ids_rejected(self):
        p = os.path.join(self.p, "d.csv")
        with open(p, "w") as f:
            f.write("id,title\nA,x\nA,y\n")
        with self.assertRaises(bs.BlindScreenError):
            bs.load_records(p)


class TestWorkflow(Base):
    def test_full_ta_workflow(self):
        bs.prepare(self.p, "ta", self.src, self.crit)
        with self.assertRaises(bs.BlindScreenError):
            bs.prepare(self.p, "ta", self.src, self.crit)  # ids fixed once prepared

        # human cannot start before Claude is sealed
        with self.assertRaises(bs.BlindScreenError):
            bs.record_human(self.p, "ta", "R00001", "INCLUDE")

        # incomplete staging refuses to seal
        sd = bs.stage_dir(self.p, "ta")
        bs.write_jsonl(os.path.join(sd, ".sealed", "staging", "batch_001.jsonl"),
                       [{"id": "R00001", "decision": "INCLUDE"}])
        with self.assertRaises(bs.BlindScreenError):
            bs.seal(self.p, "ta", "test-model")

        # EXCLUDE without code is invalid
        bs.write_jsonl(os.path.join(sd, ".sealed", "staging", "batch_001.jsonl"),
                       [{"id": "R%05d" % i, "decision": "EXCLUDE"} for i in range(1, 7)])
        with self.assertRaises(bs.BlindScreenError):
            bs.seal(self.p, "ta", "test-model")

        claude = ["INCLUDE", "INCLUDE", "EXCLUDE", "EXCLUDE", "UNSURE", "EXCLUDE"]
        human = ["INCLUDE", "EXCLUDE", "EXCLUDE", "EXCLUDE", "INCLUDE", "INCLUDE"]
        self.stage_claude(claude)
        info = bs.seal(self.p, "ta", "test-model")
        self.assertEqual(info["n"], 6)
        self.assertEqual(os.listdir(os.path.join(sd, ".sealed", "staging")), [])
        with self.assertRaises(bs.BlindScreenError):
            bs.seal(self.p, "ta", "again")

        for i, d in enumerate(human):
            bs.record_human(self.p, "ta", "R%05d" % (i + 1), d, "E2" if d == "EXCLUDE" else "")
        # undo + redo still counts once
        bs.record_human(self.p, "ta", "R00006", "UNDO")
        with self.assertRaises(bs.BlindScreenError):
            bs.reconcile(self.p, "ta")  # R00006 now undecided
        bs.record_human(self.p, "ta", "R00006", "INCLUDE")

        stats, lock = bs.reconcile(self.p, "ta", date="2026-11-04")
        self.assertEqual(stats["n"], 6)
        # binary: human I E E E I I ; claude I I E E I(unsure) E
        self.assertEqual(stats["percent_agreement_binary"], round(100 * 4 / 6.0, 1))
        self.assertEqual(sorted(lock["maybe_ids"]), ["R00002", "R00005", "R00006"])
        self.assertTrue(os.path.exists(os.path.join(self.p, "2_Screening", "round2_2026-11-04.tsv")))

        with self.assertRaises(bs.BlindScreenError):
            bs.record_human(self.p, "ta", "R00001", "EXCLUDE", "E1")  # locked
        with self.assertRaises(bs.BlindScreenError):
            bs.record_adjudication(self.p, "ta", "R00001", "INCLUDE")  # not in queue
        with self.assertRaises(bs.BlindScreenError):
            bs.record_adjudication(self.p, "ta", "R00002", "UNSURE")  # final must be I/E
        with self.assertRaises(bs.BlindScreenError):
            bs.finalize(self.p, "ta")  # pending

        bs.record_adjudication(self.p, "ta", "R00002", "EXCLUDE", "E3")
        bs.record_adjudication(self.p, "ta", "R00005", "INCLUDE")
        bs.record_adjudication(self.p, "ta", "R00006", "EXCLUDE", "E1")
        final, prisma, mpath = bs.finalize(self.p, "ta")
        self.assertEqual(prisma["records_screened"], 6)
        self.assertEqual(prisma["included"], 2)  # R1 agreed, R5 adjudicated
        self.assertEqual(prisma["excluded"], 4)
        with open(mpath) as f:
            txt = f.read()
        self.assertIn("test-model", txt)
        self.assertIn(info["sha256"], txt)

        with open(os.path.join(self.p, "2_Screening", "round3_2026-11-04.tsv")) as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        self.assertEqual([r["round3_decision"] for r in rows],
                         ["INCLUDE", "EXCLUDE", "EXCLUDE", "EXCLUDE", "INCLUDE", "EXCLUDE"])

    def test_tampering_detected(self):
        bs.prepare(self.p, "ta", self.src, self.crit)
        self.stage_claude(["INCLUDE"] * 6)
        bs.seal(self.p, "ta", "m")
        path = os.path.join(bs.stage_dir(self.p, "ta"), ".sealed", "claude_decisions.jsonl")
        os.chmod(path, stat.S_IRWXU)
        with open(path, "a") as f:
            f.write("\n")
        for i in range(6):
            bs.record_human(self.p, "ta", "R%05d" % (i + 1), "INCLUDE")
        with self.assertRaises(bs.BlindScreenError) as cm:
            bs.reconcile(self.p, "ta")
        self.assertIn("SEAL BROKEN", str(cm.exception))

    def test_fulltext_prepare_and_pdf_matching(self):
        r3 = os.path.join(self.p, "round3.tsv")
        with open(r3, "w") as f:
            f.write("record_id\ttitle\tpmid\tdoi\tround3_decision\n"
                    "R00001\tA\t1000\t10.1/x0\tINCLUDE\nR00002\tB\t1001\t\tEXCLUDE\n"
                    "R00003\tC\t1002\t\tINCLUDE\n")
        pdfs = os.path.join(self.p, "pdfs")
        os.mkdir(pdfs)
        open(os.path.join(pdfs, "Smith2020_PMID1000.pdf"), "w").close()
        with self.assertRaises(bs.BlindScreenError):
            bs.prepare(self.p, "ft", r3, self.crit, pdf_dir=pdfs)
        cfg = bs.prepare(self.p, "ft", r3, self.crit, pdf_dir=pdfs, allow_missing=True)
        self.assertEqual(cfg["n_records"], 1)
        self.assertEqual([x["id"] for x in cfg["not_retrieved"]], ["R00003"])
        self.assertEqual(bs.load_stage_records(self.p, "ft")[0]["pdf"], "Smith2020_PMID1000.pdf")
        # criteria has no F codes -> defaults
        self.assertIn("F1", cfg["codes"])


if __name__ == "__main__":
    unittest.main()
