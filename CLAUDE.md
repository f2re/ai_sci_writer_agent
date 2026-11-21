# Scientific Writer

@AGENTS.md

Read `agents/00-orchestrator.md`, then load only the files returned by `sci-writer next`.
Keep routine reads and searches scoped to the current task; do not load `research-archive/`.
The project `.claude/settings.json` denies built-in file-tool reads of that historical archive.
For an explicitly requested archive audit, explain that the project owner must change that
specific deny rule; do not use another tool to bypass it. Current instructions and `runs/` are not denied.
Use the CLI or the optional MCP transport. Follow the actual execution and review profiles.
This pointer does not install skills or grant tool permissions. See `docs/compatibility.md`.
