# Quantitative Skill Screening Methodology

## When to use

When tasked with screening a large skill library (100+ skills) for sublation candidates, use this quantitative scoring method before manual evaluation. It surfaces high-script-density skills that manual scanning would miss.

## Scoring Formula

```python
score = (
    (total_scripts * 10) +           # script count — most important signal
    (python_scripts * 5) +           # Python scripts weighted higher (more extractable)
    (total_script_size_kb) +         # raw code volume
    (10 if has_api else 0) +         # API/MCP presence bonus
    (5 if has_tests else 0) -        # test coverage bonus
    (100 if already_sublated else 0) # already-governed → strong demotion
)
```

## Data Collection

For each skill directory, collect:
- `total_scripts`: count of `.py` + `.sh` + `.js` + `.ts` files (recursive)
- `python_scripts`: count of `.py` files only
- `total_script_size_kb`: sum of all script file sizes in KB
- `has_api`: True if SKILL.md mentions api/mcp/endpoint/http/curl/fetch/requests
- `has_tests`: True if directory contains `test*.py` files
- `already_sublated`: True if directory contains `PORT_NOTES.md`

## Three-Pass Screening

### Pass 1: Strict (top 20-30 by score)
Only skills with high script density + API + tests pass. This catches heavy hitters (e.g., `last30days` at 76 scripts, `deep-analysis` at 176 scripts).

### Pass 2: Relaxed (score > 50, not sublanted)
Include moderately-scored skills. Check if "already consolidated" skills actually went through the sublation governance framework — absorption by another skill ≠ sublation governance.

### Pass 3: Governance gap (any skill with ≥1 actionable improvement)
Even low-script skills can benefit from:
- Adding PORT_NOTES.md (exists? → skip)
- Adding inventory.md (what capabilities exist?)
- Adding observability hooks
- Documentation cleanup

## Pitfalls

1. **Premature closure**: Declaring "only N candidates" after Pass 1. The user may say "多筛选几批" — this means keep going.
2. **Consolidation confusion**: A skill that absorbed others (`absorbs:` in YAML) may still lack sublation governance artifacts. Absorption ≠ sublation.
3. **Well-maintained ≠ sublanted**: External skills with tests and CHANGELOGs still benefit from PORT_NOTES and inventory.
4. **JS-only skills**: Skills with only `.js`/`.ts` files (e.g., canghe family) are config/plugin wrappers — low extraction value. Demote in manual review.
5. **Archived/superseded skills**: Skip skills marked `superseded` or absorbed into umbrella skills.

## Example: 2026-06-22 Screening

378 skills scanned → top 50 by score:

| Rank | Score | Skill | Scripts | API | Tests | Sublated | Verdict |
|------|-------|-------|---------|-----|-------|----------|---------|
| 1 | 1917 | last30days | 76 | Y | Y | N | Candidate |
| 2 | 497 | speech-to-text | 18 | Y | N | N | Already consolidated |
| 3 | 488 | contract-copilot | 17 | Y | N | N | Absorbed by contract-review |
| 4 | 488 | contract-review | 18 | N | N | N | Candidate |
| 5 | 469 | comfyui | 17 | Y | Y | N | Well-governed, skip |
| 6 | 451 | media-toolkit | 18 | Y | N | N | Already consolidated |
| ... | ... | ... | ... | ... | ... | ... | ... |

Pass 1 produced 4 candidates. Pass 2 added 4 more. Pass 3 added 2 from content-creation dedup. Final pool: 10 candidates across 4 batches.
