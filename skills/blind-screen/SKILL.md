---
name: blind-screen
description: Use when screening records for a systematic review with Claude as a truly independent, blinded second reviewer. Claude screens every record first and seals its decisions (SHA-256); the human screens blind in a fast local keyboard page; then reconcile reports Cohen's kappa and only the disagreements go to the human to decide. Title/abstract and full-text stages. Writes medsci-skills round1/round2/round3 TSVs, PRISMA counts and a Methods paragraph. Use with /meta-analysis Phase 3.
metadata:
  triggers: "blind screen, blinded screening, independent screening, dual screening, second reviewer, screen abstracts, title abstract screening, full text screening, reconcile screening, kappa, disagreements"
---

# Blind Screen — independent human + Claude dual review

This skill replaces Phase 3 rounds 3a–3c (and the dual-review part of 3d) of `/meta-analysis` with a
design a referee can check: **Claude decides first and its answers are sealed before the human
starts, the human cannot see them while screening, and the human makes every final decision.**

Tool: `python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" <command> --stage ta|ft` run from the review's
project folder (the one holding `2_Screening/`). Standard-library Python only.

## The blinding rules (you must follow these)

1. **Never read, print, summarise or hint at** anything under `2_Screening/blind/*/.sealed/` or
   `batches/` output, or Claude's decisions, until `reconcile` has run for that stage. Use
   `bs.py check` to see progress; it shows counts, never decisions.
2. **Never read `human_decisions.jsonl`** before reconcile, and never pass it to a sub-agent.
3. Claude's screening runs in **sub-agents**, not in this conversation, so its reasoning never
   appears in the chat the human reads.
4. While the human is screening, **do not discuss whether any specific record is eligible.** If
   they ask, say it would break independence and suggest pressing `?` (unsure) so it goes to
   adjudication. General questions about the criteria are fine.
5. The human screens only after `seal`. The tool enforces this, and that order is what makes the
   Methods claim true. Never edit, re-seal or delete sealed files, logs or `lock.json`.
6. Claude never makes a final decision. Agreed records follow agreement; every disagreement or
   "unsure" goes to the human.

## Workflow

### 0. Before you start
- The protocol (`/meta-analysis` Phase 1) is written and, ideally, registered.
- Phase 2 has produced one deduplicated records file (CSV/TSV with title+abstract columns, RIS,
  PubMed `.nbib`, or JSONL). Ids: an `id`/`record_id` column is kept; otherwise `R00001…`.

### 1. Write the criteria file and get it approved
Create `2_Screening/criteria_ta.md` from the protocol: PICO/PIRD, inclusion criteria, exclusion
criteria, and the exclusion codes, one per line, in exactly this form (up to 9, keyboard keys 1–9):

```
- E1: Not the target population (…)
- E2: Not the index test / intervention (…)
- E3: Review, editorial, letter, case report, conference abstract without data
- E4: No relevant outcome / reference standard
- E5: Animal or in-vitro study
```

Show it to the human and **wait for approval**. It is frozen into the stage when prepared.

### 2. Prepare and split
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" prepare --stage ta --records <deduped file> --criteria 2_Screening/criteria_ta.md
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" batches --stage ta --size 50
```

### 3. Claude's independent pass (sub-agents)
Launch one `general-purpose` sub-agent per batch file (up to 5 at a time, in the background), with
the prompt below, filling in the paths. Do not read their output files. When all have finished,
run `bs.py check --stage ta`; re-dispatch any batch with missing or problem records (the sub-agent
overwrites that batch's output file). Then seal with the **exact model id** you are running as:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" seal --stage ta --model claude-opus-5-5
```

Tell the human: number sealed, the SHA-256, and that it is now their turn.

**Sub-agent prompt (title/abstract):**

> You are an independent reviewer screening records for a systematic review. Read the eligibility
> criteria in `<project>/2_Screening/criteria_ta.md`. Read the records in
> `<project>/2_Screening/blind/ta/batches/<batch>.jsonl` (one JSON object per line). Do not open any
> other file under `2_Screening/`.
>
> For every record decide, from its title and abstract only:
> - `INCLUDE` — plausibly meets every criterion, or nothing in it rules it out.
> - `EXCLUDE` — at least one criterion is clearly failed; give the single best exclusion code.
> - `UNSURE` — the abstract is missing or too thin to judge, or it hinges on a detail only the full
>   text would show.
> Title/abstract screening is deliberately inclusive: when in genuine doubt, do not exclude.
> Judge each record on its own; do not look anything up.
>
> Write the file `<project>/2_Screening/blind/ta/.sealed/staging/<batch>.jsonl` with exactly one line
> per record, in the same order, each a JSON object:
> `{"id": "<id>", "decision": "INCLUDE|EXCLUDE|UNSURE", "code": "<E-code or empty>", "reason": "<one sentence citing the deciding criterion>"}`
> Reply only with: the number of records written.

### 4. The human screens
Run the page in the background and give the human the link it prints (it also opens their browser):
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" serve --stage ta
```
Keys: `I` include · `?` unsure · `1`–`9` exclude with that code · `Z` undo · `←/→` move ·
`G` next undecided · `N` note. Every keypress is saved, so they can stop and resume (`serve` again).
`bs.py status --stage ta` shows progress. Wait until they say they have finished.

### 5. Reconcile, then they resolve the disagreements
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" reconcile --stage ta
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" serve --stage ta --adjudicate
```
Reconcile verifies the seal, locks the human log, writes `round1_<date>.tsv`, `round2_<date>.tsv`
(with `round2_tag` INCLUDE/EXCLUDE/MAYBE) and `kappa_<date>.json`. Report agreement and κ. You may
now discuss individual disagreements if the human asks; the page shows both reviewers' reasons.
Kappa below 0.6 means the criteria are being read differently: say so and offer to look at the
pattern of disagreements before they adjudicate.

### 6. Finalise
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bs.py" finalize --stage ta
```
Writes `round3_<date>.tsv` (final decision, who resolved it, exclusion code),
`prisma_counts_<date>.json` and `methods_blind_screening_<date>.md`. Then continue with
`/fulltext-retrieval` for the `round3_decision = INCLUDE` records, saving PDFs into `pdfs/` named by
record id, PMID or DOI.

### 7. Full-text stage (same steps, `--stage ft`)
- Criteria file `2_Screening/criteria_ft.md` with F-codes (`- F1: …`). If it has none, the defaults
  are F1 no extractable outcome data, F2 no comparative/reference-standard data, F3 cannot separate
  the target population, F4 ineligible design/sample/follow-up, F5 overlapping cohort.
- `prepare --stage ft --records 2_Screening/round3_<date>.tsv --criteria 2_Screening/criteria_ft.md --pdf-dir pdfs`
  It refuses while PDFs are missing; `--allow-missing` records them as *reports not retrieved*.
- Sub-agent prompt as above, but: read each record's PDF at `<pdf_dir>/<pdf>` with the Read tool
  (use `pages` for long PDFs; include the methods and results tables), decide `INCLUDE` / `EXCLUDE`
  with an F-code / `UNSURE`, and cite the page in the reason. Use batches of 5–10.
- The page shows the PDF beside the decision panel. Finalise writes `fulltext_round3_<date>.tsv`,
  `fulltext_screening.tsv` (what `/meta-analysis` 3f `screening_reconcile.py` reads),
  `fulltext_prisma_counts_<date>.json` and the full-text Methods paragraph.

## Outputs and where they feed in

| File | Used by |
|---|---|
| `2_Screening/round1_/round2_/round3_<date>.tsv` | `/meta-analysis` 3a–3c, PRISMA counts |
| `2_Screening/kappa_<date>.json` | Methods (κ at title/abstract) |
| `2_Screening/fulltext_screening.tsv` | `/meta-analysis` 3f reconciliation gate |
| `2_Screening/*prisma_counts_<date>.json` | `/make-figures` PRISMA 2020 flow diagram |
| `2_Screening/*methods_blind_screening_<date>.md` | `/write-paper` Methods, study selection |

`seal.json` and `lock.json` in each stage folder are the audit trail: keep them with the review
data and offer them as a supplementary file.

## If something goes wrong
- *"SEAL BROKEN"*: a sealed file changed. Stop; do not use that stage. Tell the human plainly.
- A batch keeps failing validation: open `bs.py check` output, fix the sub-agent prompt (usually a
  missing E-code on EXCLUDE), and re-dispatch only that batch.
- Wrong criteria noticed after prepare: the stage cannot be re-prepared in place. Start a fresh
  project folder and record the change as a protocol deviation.
