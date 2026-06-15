# Codex Context Budget And Project Memory Policy

Start each new Codex thread from `HANDOFF_CURRENT.md`.

## Default Startup Context

1. `HANDOFF_CURRENT.md`
2. `AGENTS.md`
3. `goal.md`, only enough to understand mission and current sprint
4. Minimal project metadata needed for commands and entrypoints

Do not use long previous Codex threads as startup context when the current handoff can resume the work.

## Hard Input Rules

- Never read `note.md` in full.
- Never read `insight.md` in full.
- Never paste full test or build logs.
- Never paste full `git diff`.
- Never paste full JSON, JSONL, notebooks, generated reports, or simulation CSVs.
- Never make implementation decisions from lossy summaries of source code.
- Read exact source ranges before editing.
- Read exact changed hunks before final review.

## Archive/Search-Only Files

Treat these as archive/search-only by default:

- `note.md`
- `insight.md`
- `md/`
- old handoffs
- run journals
- trace manifests
- JSONL event streams
- large generated logs
- generated reports
- notebooks
- simulation result CSVs
- model artifact directories

Use targeted search instead:

```powershell
rg -n "specific term" note.md
rg -n "specific term" insight.md
rg -n "^#|^##|Current|Next|Validation|Failure|Result" path/to/long-md-file.md
Get-Content path/to/log.txt | Select-Object -Last 80
git diff --stat
git diff -- path/to/file
```

## Large Output Policy

- If a command may produce more than 200 lines, save raw output to a file and show only filtered evidence.
- For tests, report failing test names, first relevant stack trace, error codes, and file names only.
- For JSON/JSONL, extract keys, counts, selected errors, or selected records.
- For logs, use `tail`, `rg "ERROR|FAIL|Traceback|Exception|panic|fatal"`, or a short parser.
- If filtered output is ambiguous, inspect the raw exact lines.

## Project Memory

- `goal.md` is the mission, current sprint, success criteria, architecture/agent roles, safety boundaries, roadmap, and project-specific quality standard.
- `HANDOFF_CURRENT.md` is the short current-state handoff for new threads.
- `note.md` is the chronological execution journal.
- `insight.md` is only for confirmed reusable improvements.

## Journal Rules

Append to `note.md` for each meaningful execution loop:

- timestamp
- part
- goal
- hypothesis
- actions
- candidates/options
- metrics
- result
- failure reason
- next action
- token usage, if available

Do not paste raw logs into `note.md`; reference log paths and key metrics only.

## Insight Rules

Append to `insight.md` only when a reusable improvement is confirmed:

- source loop
- improvement
- before
- after
- evidence
- remaining risk

Do not add routine loop completions, speculative ideas, diagnostic-only runs, or ordinary failures to `insight.md`.

## Closeout Rules

At the end of each small part:

- run the smallest relevant validation first, then any broader relevant validation;
- inspect `git diff --stat`;
- inspect only exact changed hunks needed for review;
- update `HANDOFF_CURRENT.md` in 10 changed/appended lines or fewer;
- append one concise event to `note.md`;
- append to `insight.md` only if there is a confirmed reusable improvement;
- record current Codex thread token usage once if `codex_ops.py` supports it in the current environment.
