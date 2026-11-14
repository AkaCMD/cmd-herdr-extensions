# Herdr task progress

Apply this guidance when `HC_TERMINAL_ID` is set. Resolve the Herdr directory from `HERDR_APP_HOME`, or the user's home directory plus `.config/herdr-app`. Read its `shared.json`; an explicit `agent-todos.auto` value of `false` disables this guidance.

At the start of a user task, create or update a checklist through the agent's native task tool. Use `update_plan` in Codex, `TodoWrite` or `TaskCreate`/`TaskUpdate` in Claude Code, `todo` in Pi, `todowrite` in OpenCode, and `todo_write` in DeepSeek Harness. Mark the active step as in progress, record completed steps after verification, and revise the checklist when the user changes the task. A request to continue resumes the existing checklist.

If Codex has no `update_plan`, pass JSON with a `plan` array of `step` and `status` objects to `mods/agent-todos/codex_plan.py` under the resolved Herdr directory. Use `pending`, `in_progress`, and `completed`, with at most one step in progress. The bridge reads the current terminal and session from the environment. If Claude has no task tool, use `mods/agent-todos/md_checklist_sync.py` with a temporary Markdown checklist and the confirmed current Claude session ID.

Keep progress updates in the task tools or bridge. Herdr reads those snapshots automatically. Continue the user's task after each update. Terminal input, interruption keys, and additional user messages are reserved for explicit user actions.
