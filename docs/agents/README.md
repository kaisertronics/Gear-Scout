# Gear Scout's helpers (agents) and who watches them

Gear Scout has three Claude helpers. Each one is just a written job description
(a "prompt") in this folder plus a schedule. Any Claude session — on the Mac,
on claude.ai, the Claude app on a phone, or Claude Code on the web — can pick
one up by reading its file, because everything lives in this GitHub repo.

| Helper | File | When | What it may change |
|--------|------|------|--------------------|
| Nightly developer | [../nightly-routine.md](../nightly-routine.md) | Nightly ~1:45 AM, on the Mac that runs Gear Scout | Code, deploys (via `scripts/safe_deploy.sh`), changelog, push to main |
| Marketing & vision director | [marketing-vision-director.md](marketing-vision-director.md) | Weekly (Monday morning) | Docs only: README, USER_GUIDE, docs/vision.md, docs/ideas.md — opened as a pull request |
| Intern | [intern.md](intern.md) | Daily (morning) | Small, safe fixes and checks — always as a pull request, never straight to main, never deploys |

## Oversight (the "manager" job)

The project's Claude coordinator is the manager. After each helper runs it:

1. Reads what the helper did (its report, its pull request, `docs/agent_log.md`).
2. Checks the hard rules were kept (below). Anything that breaks one is
   reverted or the pull request is closed with a note.
3. Merges good intern/director pull requests that are docs-only or clearly
   safe; anything that changes how the owner uses the app goes to the owner
   first as a yes/no question.
4. Notices when a helper has stopped running (no new entry in
   `docs/agent_log.md` / `docs/quality_log.md` for 2+ days) and tells the owner.
5. Sends the owner one short plain-language summary, not one per helper.

## Hard rules every helper follows

- Read `CLAUDE.md` first; it wins over anything here.
- Never commit `config.yaml` or `.env`; never print or log passwords, keys or the ntfy topic. The repo is public.
- No Claude/AI attribution in commits or code.
- Only the nightly developer deploys, and only with `scripts/safe_deploy.sh`.
- Don't delete the owner's data (favorites, hidden, Telex List, settings).
- No paid Anthropic API key features; Gemini on the owner's free key is fine.
- Be gentle with Facebook and eBay; never bypass captchas, logins or bot protection.
- Don't spend money, create accounts, or post/send anything outside the repo
  (social media, emails, forums) without the owner's yes.

## Shared log

Each helper adds one line to the top of [../agent_log.md](../agent_log.md):
date, helper, what it did, link to its pull request or commit. This is how the
manager (and the owner) can see at a glance that everyone is still working.

## Reaching Gear Scout remotely

- **The app itself** runs on the owner's Mac in Docker. From a phone or any
  other computer, open the ngrok link (see USER_GUIDE.md → "Remote access").
  Alerts arrive by ntfy push and email wherever the owner is.
- **The helpers and the code** live in GitHub (kaisertronics/Gear-Scout) and
  in the claude.ai "Gear scout" project, so they're reachable from the Claude
  app on phone, desktop and web on any account that has access to the project
  and the repo.
- Cloud helpers (director, intern) can't see the Mac's running app or its
  database — they work on the code and docs only. Anything that needs the live
  app (accuracy audit, deploys, health checks) is the nightly developer's job
  on the Mac.
