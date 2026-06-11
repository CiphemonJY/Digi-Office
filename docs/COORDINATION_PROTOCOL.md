# Coordination Protocol v2

**Supersedes** the GSD-era rule "git commits (primary), Taildrop (files), A2A ping
(urgent status only)". Every fleet agent must carry this block in its standing
prompt / CLAUDE.md / cron prompt. The canonical copy lives here; hosts pull it.

---

The Digi-Office coordinator is the **primary and mandatory** coordination
channel. Git is for code; the coordinator is for work.

1. **Before starting any unit of work** (eval run, training job, ontology pass,
   doc rewrite, fix), there must be a coordinator task for it. If one wasn't
   dispatched to you, create it yourself (`POST /tasks`) and claim it —
   10 seconds of overhead, non-negotiable.
2. **While working**: report progress at meaningful checkpoints
   (`POST /tasks/{id}/heartbeat` with a `log_entry`).
3. **When done**: complete the task **with a structured result**
   (`POST /tasks/{id}/complete`, `result_payload` = the actual numbers/paths,
   never just "done"). If it failed, fail it with the real error — a crash
   recorded as success is worse than a failure.
4. **A2A messages** go through `/a2a/messages` — not git commit messages, not
   Taildrop notes.
5. **Work that never appeared as a task is DARK WORK.** The harness hooks
   report your tool activity automatically, the office renders untracked
   activity with an amber `⚠ off-book` flag, and the monitor reconciles
   commits against tasks. Dark work does not count as done for any sprint or
   goal — if it isn't in the coordinator, it didn't happen.

Git commits remain required for code changes, but a commit is the **artifact of
a task, not a substitute for one**. Reference the task id in the commit message:
`[task:<first-8-chars>]`.

---

## Dark-work reconciliation (monitor rule)

Appended to the fleet monitor's cron prompt:

> Each run: pull the latest commits on LISA_FTM and Digi-Office since your last
> run. For each commit, check whether its author agent has a plausibly matching
> coordinator task (claimed/completed in the same window, or a `[task:xxxxxxxx]`
> tag in the message). Commits with no matching task = dark work: send the
> author an A2A nudge naming the commit and the rule, and log one feed event
> (`kind='dark_work'`). Three dark-work flags for the same agent in a week →
> escalate to James. Do not nudge for merge commits, hb/auto commits, or
> commits by James himself.

## Auto-reporting (harness hooks)

Each host installs `scripts/claude_hooks/digi_report.py` as a Claude Code
`PostToolUse` / `SessionStart` / `Stop` hook (see the README "Auto-reporting
hooks" section for the settings.json snippet and env vars). Reporting is done
by the harness unconditionally — it does not depend on the model remembering.
