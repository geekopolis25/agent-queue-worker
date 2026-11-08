# agent-queue-worker

Polls the Todoist Agent Queue project (section: Queued for Agent, label: track2), runs each Dispatch Brief through the Anthropic API, posts the Return Block as a comment, and moves the card to Review.

Secrets (GitHub repo settings > Secrets, never commit): ANTHROPIC_API_KEY, TODOIST_API_TOKEN. Optional: MODEL.

Test without writes: `python worker.py --dry-run`
Parse a local brief: `python worker.py --parse-file brief.md`

Runs on GitHub Actions every 15 min. Dry-run by default. Set repo variable LIVE=true to go live.
