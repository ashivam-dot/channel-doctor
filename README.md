# Channel Doctor

A free, always-on caretaker for the YouTube channel automations. It runs entirely on GitHub Actions (public repos,
no paid service, no laptop) and talks to the owner through the Telegram bot **@AkshChannelDoctorBot**.

| Part | When | What it does |
|---|---|---|
| Heal (`doctor.yml`, reusable) | the moment a watched workflow fails in a channel repo | reads the failed job's log; billing/credential problems go straight to Telegram; quota waits; temporary errors are re-run (up to 3 attempts); anything else goes to Gemini for a diagnosis and, in `fix` lanes, a small patch |
| Watch (`watch.yml`) | every 15 min | every channel's latest public upload (YouTube feed), schedule lateness, 3-failure streaks, disabled workflows, open `alert` issues, the doctors themselves; Telegram on new / still open (12 h) / resolved; daily digest 21:00 IST; bot commands |
| Review (`review.yml`) | Mon and Thu 09:00 IST | per-channel review from public numbers, learnings and CHANNEL.md; `reports/review-<date>.md` and `reports/review-latest.json` |
| Improve (each repo's `doctor.yml` schedule) | Tue and Fri 09:53 IST | applies the review's safe, testable code recommendations (`auto_ok`) in `fix` lanes |
| Scout (`scout.yml`) | Wed and Sun 09:30 IST | new and rising GitHub repos and Hugging Face models for the studios' needs, licence-filtered, ranked against the stack; `reports/scout-<date>.md` |

## Guard rails for code changes

- Only `fix` lanes change code, and only files matching the lane's `editable` globs. Workflows, tests, docs, JSON,
  lock files, `pyproject.toml`, `state/` and secrets are never edited.
- At most 4 files and 80 changed lines per patch, every `find` snippet unique, model confidence at least 0.6.
- The repo's own test command must pass before anything is pushed; one retry with the test output, then it gives up.
- At most 3 changes per repo per day; the same failure is never retried by AI within 24 h.
- If the watched workflow fails again after a doctor change, the change is reverted automatically and the owner is told.
- Logs are redacted before they reach Gemini, Telegram or issues. The doctor never reads secret values.
- Hands-off channels (Universe Receipts) are watched from public data only and get written suggestions only.

## Controls

- Repository variable `DOCTOR_PAUSED=true`: stops the doctor in that repo (here: stops watch, review and scout).
- Repository variable `DOCTOR_MODE=report` in a channel repo: diagnose and alert, never change code.
- Telegram: `/status`, `/digest`, `/review`, `/scout`, `/help` (answered within about 15 minutes).
- Each channel repo has a **Doctor log** issue with every decision; this repo has **Doctor state** and **Scout memory**.

## Adding a channel

1. Add it to `channels.json` (`mode`: `fix` needs `test` and `editable`; `heal` only re-runs and diagnoses).
2. `python -m doctor caller <repo>` prints the repo's `.github/workflows/doctor.yml`; commit it there.
3. Set the repo secrets `DOCTOR_GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Tests: `python -m pytest -q`.
