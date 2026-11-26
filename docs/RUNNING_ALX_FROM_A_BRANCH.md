# Running AL/X from an unmerged branch

While a PR is open, AL/X runs its head from a code snapshot outside iCloud. Her own self-restart
refuses while she runs a branch main doesn't hold (`running_unmerged_work`), so a Claude session
restarts her.

1. **Ask Friedl first.** Check she is idle: the last `Reasoning provider request` line in
   `.alx/runtime/live_voice.log` is not recent, no `codex exec` is running, and no board job is
   calling (`.alx/runtime/board-jobs/<job>.calls.jsonl`). She resumes open jobs on restart, so ask
   Friedl to have her close any job that should not carry on.
2. Make the snapshot:

   ```sh
   SHA=$(git rev-parse HEAD)
   B="$HOME/Library/Application Support/alx/runtime-code"
   mkdir -p "$B/$SHA"
   git archive $SHA src config LAWS_OF_ALX.md IDENTITY_AND_MEMORY.md | tar -x -C "$B/$SHA"
   touch "$B/$SHA/.complete"
   ```

3. Stop her: `kill -TERM <pid>` (the one process listening on port 8765:
   `lsof -nP -iTCP:8765 -sTCP:LISTEN -t`) and wait for it to exit.
4. Start her detached, so she survives the Claude session. Keep the board-agent settings she
   already runs with (read them from the old process with `ps eww <pid>`):

   ```python
   import subprocess, os
   root = "/Users/friedlbasson/Desktop/VSC/AL_X"
   code = os.path.expanduser("~/Library/Application Support/alx/runtime-code/<SHA>")
   env = dict(os.environ, PYTHONPATH=code + "/src", ALX_BOARD_AGENT_PROVIDER="codex_subscription",
              ALX_BOARD_AGENT_MODEL="gpt-6.2-sol", ALX_BOARD_AGENT_EFFORT="high")
   log = open(root + "/.alx/runtime/live_voice.process.log", "a")
   p = subprocess.Popen(["python3", "-m", "alx.bootstrap." + "live_voice", "--checkout", root], cwd=root,
                        env=env, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT)
   print(p.pid)
   ```

5. Verify: port 8765 is listening, exactly one process, and its PYTHONPATH names the new SHA.
   Never put the live_voice module name in a command that also checks status.

## Evidence

- Board jobs: `.alx/runtime/board-jobs/<job>.json`, `.calls.jsonl`, `.reads.json`.
- Goals: `.alx/runtime/goals.sqlite3` (`state_json`).
- Trace: `.alx/runtime/trace.sqlite3`. Self-restarts: `.alx/runtime/self-restart.log`.
