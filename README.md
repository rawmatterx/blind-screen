# Blind Screen

**Independent, blinded human + Claude dual screening for systematic reviews — so one person can run a review that still meets the two-reviewer standard.**

Systematic reviews expect two reviewers to screen every record independently. Solo researchers
either skip that or let an AI "help", which usually means seeing its answer first — not
independent at all. Blind Screen makes the AI a genuine second reviewer:

1. **Claude screens every record first**, in sub-agents, so its reasoning never appears in your chat.
   Its decisions are written to a file and **sealed with a SHA-256 fingerprint** before you begin.
2. **You screen blind** in a fast local page — one record at a time, keyboard only, ~300 abstracts an hour.
   The page refuses to show Claude's decisions until you have finished.
3. **Reconcile** verifies the seal, locks your log, reports **Cohen's κ**, and shows you **only the
   disagreements**, with both reviewers' reasons side by side. **You make every final decision.**
4. **Finalise** writes the screening tables, PRISMA counts and a **Methods paragraph** that describes
   exactly what was done.

Title/abstract and full-text stages (full text shows the PDF beside the decision panel).

![workflow](https://img.shields.io/badge/order-Claude%20sealed%20→%20you%20screen%20blind%20→%20κ%20→%20you%20adjudicate-555)

---

## Install

Requires [Claude Code](https://claude.com/claude-code) and Python 3.9+. No other dependencies.

**As a Claude Code plugin**
```
/plugin marketplace add rawmatterx/blind-screen
/plugin install blind-screen@blind-screen
```

**Or copy the skill**
```bash
git clone https://github.com/rawmatterx/blind-screen.git
cp -R blind-screen/skills/blind-screen ~/.claude/skills/
```

Then, in a review's project folder, ask Claude to *"blind-screen my search results"*, or type `/blind-screen`.

## Using it

Claude drives the commands for you; this is what happens underneath (`bs.py` lives in the skill's `scripts/`):

```bash
bs.py prepare   --stage ta --records search_dedup.ris --criteria 2_Screening/criteria_ta.md
bs.py batches   --stage ta --size 50        # Claude's sub-agents screen each batch
bs.py check     --stage ta                  # counts only, never decisions
bs.py seal      --stage ta --model claude-opus-5-5
bs.py serve     --stage ta                  # you screen in the browser
bs.py reconcile --stage ta                  # seal verified, κ, round1/round2 tables
bs.py serve     --stage ta --adjudicate     # you resolve the disagreements
bs.py finalize  --stage ta                  # round3 table, PRISMA counts, Methods text
```

**Keys in the page:** `I` include · `?` unsure · `1`–`9` exclude with that reason · `Z` undo ·
`←/→` move · `G` next undecided · `N` note. Every keypress is saved immediately; stop and resume any time.

**Inputs:** CSV/TSV (title + abstract columns), RIS, PubMed `.nbib`/MEDLINE, or JSONL. Exclusion codes
come from your criteria file as lines like `- E1: Not the target population` (up to 9).

**Full text:** `prepare --stage ft --records 2_Screening/round3_<date>.tsv --pdf-dir pdfs/`. PDFs are
matched by record id, PMID or DOI in the filename; missing ones can be recorded as *reports not retrieved*.

### Outputs

| File | What it is |
|---|---|
| `2_Screening/round1_<date>.tsv` | Your decisions |
| `2_Screening/round2_<date>.tsv` | Both reviewers + `round2_tag` (INCLUDE / EXCLUDE / MAYBE) |
| `2_Screening/round3_<date>.tsv` | Final decision, who resolved it, exclusion code |
| `2_Screening/kappa_<date>.json` | κ (binary and three-category), % agreement, disagreement counts |
| `2_Screening/prisma_counts_<date>.json` | Counts for the PRISMA 2020 flow diagram |
| `2_Screening/methods_blind_screening_<date>.md` | Ready Methods paragraph, including the seal hash |
| `2_Screening/blind/<stage>/seal.json`, `lock.json` | Audit trail — keep with your review data |

The table formats follow [medsci-skills](https://github.com/Aperivue/medsci-skills)' `/meta-analysis`
Phase 3, so its downstream gates (`screening_reconcile.py`, PRISMA consistency checks) read them unchanged.
Blind Screen works on its own too.

## What the tool enforces

- **Order.** You cannot start screening until Claude's decisions are sealed, so Claude provably
  could not have seen yours.
- **Blinding.** The page never serves Claude's decisions before reconcile; the skill instructs Claude
  not to read, summarise or hint at them, and not to discuss specific records while you screen.
- **Integrity.** Reconcile and finalise refuse to run if the sealed file or your locked log changed.
- **Fixed ids.** A stage cannot be re-prepared in place; a changed search means a new project folder.
- **Human authority.** Agreed records follow agreement; every disagreement or "unsure" goes to you.
  Claude never makes a final decision.

## Limits — read before you rely on it

- **A local hash proves the file did not change, not *when* it was made.** For an independent timestamp,
  post the seal's SHA-256 somewhere public before you start screening — e.g. a commit, an OSF project
  update, or an amendment note in your PROSPERO record.
- **LLM screening accuracy is not validated by this tool.** Sensitivity varies with topic, criteria wording and
  model version. Report the model version, the prompt (in `SKILL.md`), and κ. Journals' policies on AI
  reviewers differ; disclose it plainly (the generated Methods paragraph does).
- **Blinding depends partly on discipline.** The tool keeps Claude's decisions out of the page and the
  chat, but it cannot stop you from opening the sealed file yourself.
- **It screens; it does not search, extract data or assess risk of bias.** Blinded dual extraction and
  risk-of-bias forms are planned.

## Tests

```bash
cd skills/blind-screen && python3 -m unittest discover -s tests -v
```
13 tests cover record parsing (CSV, RIS, MEDLINE), PDF matching, κ against a known value, the
seal-before-screen order, tamper detection, locking, adjudication rules and final counts.

## Contributing

Issues and pull requests are welcome — especially validation data (κ and sensitivity of the Claude pass
against completed human dual-screened reviews), new import formats, and the extraction/risk-of-bias stages.
Please never attach real screening data from an unpublished review.

## License

MIT — see [LICENSE](LICENSE).
