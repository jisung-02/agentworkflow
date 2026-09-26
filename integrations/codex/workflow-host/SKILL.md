---
name: workflow-host
description: Work on queued codex-host tasks from the local YAML workflow engine and report results through its CLI. Use when the user asks to handle a queued workflow task or check workflow status in Codex.
---

# Workflow host

Use the installed `workflow` command from the user's workflow workspace. The default database is `.workflow/state.db` in that workspace; pass `--db PATH` when the user uses another database.

For status, run `workflow status RUN_ID`. To accept one queued task, run `workflow host-next --runner codex-host`. This claims the task and prints JSON containing its token ID, instructions, inputs, previous outputs, workdir, and permitted outcomes. If it prints `null`, no task is waiting.

Carry out the task in its `workdir` using the instructions and provided context. Keep the token ID. Once the work is complete, write a concise factual result to a UTF-8 file and call `workflow host-complete TOKEN_ID OUTCOME --output-file PATH` with one of the permitted outcomes. If you cannot tell whether an interrupted task changed files or external state, report the uncertainty to the user before submitting a result. Do not claim another task while one is in progress.

For work lasting more than an hour, run `workflow host-heartbeat TOKEN_ID` periodically so the two-hour task lease remains valid.

To start a new run from a YAML file, use `workflow run FILE --input NAME=VALUE`. To answer a waiting human state, use `workflow respond TOKEN_ID OUTCOME` after the user supplies that outcome.
