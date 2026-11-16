---
name: paper-worker
description: Delegate paper-writing sub-tasks to the local paper_worker agent, which runs on the user's Claude API credit instead of Claude Code usage. Use when the user asks to have the paper worker (or "API worker") find references, analyze papers, write the Introduction's related-work review, or analyze experimental data and draft results text.
---

# paper-worker

`paper_worker.py` is a local agent (Claude API) with workspace-scoped file tools, OpenAlex paper search, web search/fetch, and local Python execution. Its output lands in files inside the paper folder.

## How to call it

Run it with Bash (PowerShell works too). It can take several minutes, so run it in the background and wait for the notification:

```bash
python "<PATH_TO>/paper_worker.py" run -w "<paper folder>" --json --task "<task>"
```

- Write a self-contained task: research topic, the paper's contribution, which files to use (`papers/`, `data/`, `drafts/abstract.md`), and the desired output file and length.
- For long task descriptions, write them to a file and pass `--task-file <file>`.
- Data tasks need local Python: add `--approve-python yes` only if the user agreed to let the worker run code unattended. Otherwise ask the user first, or have the user run the worker in `chat` mode themselves.
- Follow-ups: pass `--resume <session_id>` from the previous JSON result so the worker keeps its context.
- Cost guard: `--max-cost` (USD, default 5). Mention the reported `usage.est_cost_usd` to the user.

## After it returns

stdout is JSON: `status`, `session_id`, `final` (a summary in Chinese), `files_written`, `usage`.
Read the files in `files_written` (typically `drafts/intro_related_work.md`, `literature/references.bib`, `literature/notes.md`, `drafts/results.md`, `figures/*`) before you report to the user. Spot-check a few citations (DOI exists, claim matches abstract) and any numbers against `analysis/results_summary.md`. Point out `[CHECK: ...]` and `[TODO: ...]` markers.
If `status` is not `ok`, report `final` (the reason) and suggest a fix, for example a higher `--max-steps`/`--max-cost` or a smaller task.
