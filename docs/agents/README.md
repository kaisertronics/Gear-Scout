# Gear Scout's helpers and who oversees them

This page describes the helpers as they actually run on the owner's Mac
(~/Documents/gear-scout), checked 2026-11-06. Nothing here adds a new helper.

## Roles

- **Vision director (project manager):** the "CONTROL GEAR SCOUT UPDATE" chat
  (see memory/pm-role.md on the Mac). It decides what to build and comes up with
  ideas. It hands the building work to cheaper builder agents.
- **Intern (builder):** a Sonnet agent created fresh for each job, using the
  Agent tool or `scripts/claude_job.sh`. It isn't saved as its own file.
- **Idea "block" routine:** `scripts/block_prompt.txt`. Once per 5-hour usage
  block it builds up to 2 queued items, records the owner's habits in
  docs/owner_patterns.md, and adds 3 new proposals (at least one big bet).

## Schedule (on the Mac)

The main runner is `scripts/routine_loop.sh` in a Terminal tab. Each job goes
through `scripts/run_routine.sh` / `scripts/claude_job.sh`, and the logs are in
~/Library/Logs/gear-scout.

| Job | When | Prompt |
|-----|------|--------|
| block | once per 5-hour usage block | scripts/block_prompt.txt |
| hourly | :27 every hour | ~/.claude/scheduled-tasks/gear-scout-hourly |
| nightly | 10:10 AM | docs/nightly-routine.md / gear-scout-nightly |
| code review | 11:30 AM | gear-scout-code-review |

The Claude desktop scheduled tasks with the same names are only a backup. Each
one stops right away while the main runner's heartbeat is fresh. A launchd
health check (com.gearscout.healthcheck) runs every 5 minutes.

## Oversight

The claude.ai "Gear scout" project coordinator watches all of this from any
device:
- Checks that the runs keep going: the newest logs, and new commits on GitHub.
- Reviews what the director and builders changed against CLAUDE.md. The most
  important rules: no secrets in commits, deploys only through
  scripts/safe_deploy.sh, and no deleting the owner's data.
- Sends the owner one short plain-language summary and flags anything that
  needs a yes or no.

**Known gap (2026-11-06):** the runs commit on the Mac but don't push. Local
main was 293 commits ahead of GitHub. Every run should end with
`git push origin main` so the work is backed up and can be seen remotely.
