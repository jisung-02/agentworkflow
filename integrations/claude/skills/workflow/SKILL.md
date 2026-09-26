---
name: workflow
description: Handle queued claude-host tasks from the local YAML workflow engine and report results through its CLI. Use when the user asks to continue a workflow inside Claude Code or inspect a workflow run.
---

# Workflow host

Use the installed `workflow` command from the user's workflow workspace. The default database is `.workflow/state.db` in that workspace; pass `--db PATH` when the user uses another database.

Run `workflow status RUN_ID` to inspect a run. When the user asks to work on the next queued Claude task, run `workflow host-next --runner claude-host`. The command claims one task and prints JSON with its token ID, instructions, inputs, previous outputs, workdir, and permitted outcomes. `null` means no task is waiting.

Complete the task in its `workdir` using those instructions. Write the result summary to a UTF-8 file, then run `workflow host-complete TOKEN_ID OUTCOME --output-file PATH` with an allowed outcome. If an interruption leaves the effects uncertain, tell the user what happened before submitting any outcome. Claim one task at a time.

For work lasting more than an hour, run `workflow host-heartbeat TOKEN_ID` periodically so the two-hour task lease remains valid.

Use `workflow run FILE --input NAME=VALUE` to start a workflow and `workflow respond TOKEN_ID OUTCOME` only after the user supplies the requested response.
