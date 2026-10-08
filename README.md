# Canvas autonomous discussion agent

This project reads one MIT Canvas discussion and can, only with explicit opt-in, reply to an existing thread. It never creates topics, edits, or deletes Canvas content.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export CANVAS_TOKEN='your-canvas-api-token'
```

`CANVAS_TOKEN` is required and is only used for Canvas requests. It is never logged, stored, printed, or sent to the LLM.

To enable real per-thread decisions, set:

```bash
export OPENAI_API_KEY='your-openai-api-key'
export OPENAI_MODEL='gpt-4.1-mini'  # optional; this is the default
```

Without `OPENAI_API_KEY`, the mock engine safely returns `SKIP`.

## Dry run (default)

Canvas writes are disabled by default. Run:

```bash
unset CANVAS_WRITE_ENABLED
python agent.py
```

The agent reads the topic and full discussion view, remembers completed/skipped IDs in `data/memory.json`, and evaluates only the five newest candidate threads in one LLM decision call per cycle. The model scores the best contribution from 0–5 and replies only at score 4 or 5: clearly novel, technically specific, actionable, or unusually discussion-advancing value. Scores 0–3 produce a standardized `SKIP`; the model's detailed rationale and score remain in `activity.log`. A dry-run `REPLY` intentionally does **not** mark its target thread seen, so the same proposal remains eligible when you later enable live writes. A model `SKIP` marks only those evaluated candidates processed; an LLM/API/validation error marks none. Replies target about 80–150 words and have a hard 200-word limit; an overlong reply gets one shortening retry before failing safely.

After model scoring, the agent skips rather than immediately replying to the same root thread as its most recently accepted Canvas contribution. It then skips before writing if three accepted contributions already occurred in the prior eight hours. These are independent restraint guards; the existing hard limit of three accepted POSTs per rolling hour still applies.

## Live write opt-in

Only enable this after reviewing a dry run:

```bash
export CANVAS_WRITE_ENABLED=true
python agent.py
```

A live reply requires all of the following:

- `CANVAS_WRITE_ENABLED=true`
- an LLM decision of `REPLY`
- a fresh, immediately pre-write metadata read whose leading description line is exactly `COURSE-TEAM CONTROL: RUNNING`
- fewer than three accepted POSTs in the previous rolling hour
- no unresolved `pending_action`

`PAUSED`, missing, malformed, or unavailable control text blocks the write. At most one contribution is attempted in a cycle. Canvas reads use short exponential-backoff retries for transient failures; authentication errors are not retried. POSTs are deliberately not blindly retried because an ambiguous acknowledgement is reconciled through the pending action instead.

Before posting, the agent atomically saves a `pending_action` containing the target entry ID and exact reply text. An accepted POST is immediately recorded against the rolling hourly limit, even if verification is delayed. After Canvas accepts the reply, it re-fetches the discussion and verifies the reply appears under the intended root entry. Only then does it mark the reply as its own and clear the pending action.

If a post acknowledgement or verification is lost, the next run checks the pending action before making any new LLM decision. It looks recursively under the intended thread for the returned reply ID first, then normalized reply text, using author identity when Canvas returned it. An unresolved action is GET-only: it blocks new posts and is never re-posted automatically.

## Lost-acknowledgement recovery test

Use a controlled live test only in a course-approved discussion with the control line set to `RUNNING`:

```bash
export CANVAS_WRITE_ENABLED=true
export SIMULATE_LOST_ACK=true
python agent.py
```

This performs one successful POST, persists the accepted-post safety state, then intentionally skips verification and completion while leaving `pending_action` intact. For the recovery run, disable the simulation:

```bash
export SIMULATE_LOST_ACK=false
python agent.py
```

The second run reconciles the existing Canvas reply, records it as agent-authored, and does not create a duplicate.

## Local state and logs

`data/memory.json` is atomically replaced to reduce corruption risk. It stores seen item IDs, verified agent reply IDs, rolling successful-write timestamps, and an optional pending action. `logs/activity.log` contains concise cycle metadata—decision, control state, rate-limit/write/verification/recovery outcomes—but no tokens, API keys, or authorization headers.

## Cron scheduler (every 2 hours)

The scheduler is intentionally a small cron wrapper. It is not installed automatically.

Create a local secrets file outside this repository:

```bash
touch ~/.canvas_agent_env
chmod 600 ~/.canvas_agent_env
```

Edit `~/.canvas_agent_env` with your own values. Do not commit or share this file:

```bash
export CANVAS_TOKEN='your-canvas-token'
export OPENAI_API_KEY='your-openai-api-key'
export OPENAI_MODEL='gpt-4.1-mini'
export CANVAS_WRITE_ENABLED='false'
export SIMULATE_LOST_ACK='false'
```

`CANVAS_WRITE_ENABLED` must stay `false` unless you have deliberately approved live posting.

Make the wrapper executable and test one scheduled-style cycle manually:

```bash
chmod +x run_agent.sh
./run_agent.sh
tail -n 50 logs/cron.log
```

When that succeeds, open your crontab:

```bash
crontab -e
```

Add this line to run every two hours:

```cron
45 */2 * * * /bin/bash "/absolute/path/to/canvas-autonomous-agent/run_agent.sh"
```

Check installed jobs with:

```bash
crontab -l
```

To disable the scheduler later, run `crontab -e` and remove (or comment out) that line, then confirm with `crontab -l`. Review scheduled output in `logs/cron.log`.

Replace the path with the absolute path to your cloned repository.

### Persistent-memory privacy

The live `data/memory.json` file is runtime-local state and is not included in the repository or submission. `data/memory.example.json` documents the persisted schema using synthetic placeholder values. The agent recreates the live memory file automatically on first run. Runtime logs are also excluded from the repository/submission; only sanitized excerpts or screenshots are used as evidence.
