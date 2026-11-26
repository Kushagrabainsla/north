# Skill picking: before and after (issue #87)

`verify_before_after.py` runs the product code of two checkouts on the same data:

- **Picks.** The checkout's `Agent._select_skills` and `Agent._load_skills_block` make the skills text for each request. Opus 5.5 (medium effort) sees that text and the request, and names the skill it would load. The truth for 168 chat requests is a judge (Opus 5.5, high effort) that read every skill of `main` in full. The 64 example requests of real drafts carry their own labels.
- **Drafts.** Each of the 25 real `create_skill` drafts goes through the checkout's `CreateSkillTool.run`, in a temporary copy of the skills folder.

Run it:

```bash
.venv/bin/python experiments/skill_picking/verify_before_after.py before <main checkout> <main checkout>
```

```bash
.venv/bin/python experiments/skill_picking/verify_before_after.py after . <main checkout>
```

The model answers are cached in `~/.north/experiments/context_learning/cache`. The results here hold counts only.

| | Before (`main`) | After |
|---|---|---|
| Chat: right skill loaded (of 50 that need one) | 36 | **47** |
| Chat: wrong skill loaded | 8 | **0** |
| Chat: no skill loaded when one was needed | 6 | 3 |
| Chat: a skill loaded when none fits (of 118) | 25 | 26 |
| Draft examples: right outcome (of 64) | 57 | **61** |
| Real drafts saved (of 25) | 3 | **21** |
| Drafts refused by the selection test | 18 | 0 |
| Skills text per task, tokens (mean) | 184 | 1,507 |
