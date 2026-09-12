# usage-coach

A terminal tool that reads [codexbar](https://github.com/steipete/CodexBar)'s
usage data and tells you **what size of job to run now, why, and when it
matters**. Its output is a recommended action, not a readout.

It cross-reads the 5-hour and 7-day limits of Claude, Codex and Antigravity into
five situations, and comes with a **rate guard** for agent loops (`--hook` /
`--guard-check`).

## The idea

- **The 5-hour limit** disappears at the reset if you do not use it — so if there
  is room, spend it. Use it or lose it.
- **The 7-day limit** is about pace.
- **The Fable 5 weekly limit** (a Claude Max subscription) is a display-only
  `Fable` line. It is kept out of both the coaching and the rate guard, and where
  there is no data for it — a standard subscription — the line is omitted
  entirely. It appears in `--json` under the `fable_7d` key.
- The comparison at the centre is `left` (the fraction remaining) against
  `timeLeft` (the fraction of the window still to run). Whichever is tighter sets
  the ceiling on job size.

## The five outputs

| | situation | recommendation |
|--|--|--|
| 🔴 | the 7-day limit is tight | hold the big jobs |
| 🟢 | 5 hours roomy, the week has room | run the big job now |
| ⏳ | 5 hours short, reset imminent | wait a little, then go big |
| 🟡 | 5 hours short, behind pace | keep the jobs small |
| ⚪ | nothing unusual | carry on as usual |

## Requirements

- Python 3, standard library only — no dependencies
- [codexbar](https://github.com/steipete/CodexBar) — where the usage data comes
  from (the `codexbar` CLI)

## Installing

```bash
git clone https://github.com/netwaif/usage-coach.git
cd usage-coach
# optional, to have it as a global command
ln -s "$PWD/coach.py" /usr/local/bin/coach
```

## Using it

```bash
coach                                    # query every provider live, and watch
coach --providers claude,codex           # only these providers
coach --once                             # print once and exit
coach --mock all                         # a mocked demo of all five outputs
coach --file samples/claude.json:claude  # read from a JSON file
coach --account antigravity:foo@gmail.com  # name an account (passed to codexbar --account, repeatable)
coach --json                             # machine output, one line of JSON, for an agent loop
```

A codexbar constraint: `--provider all` hangs, so each provider is queried
separately — coach does that for you. codex uses `--source cli`.

## The rate guard (for agent loops)

Lets a long autonomous loop watch its own usage and **stop itself when that gets
risky**.

- `coach --hook` — for a Claude Code Stop hook, on the 7-day guard. A stop prints
  `{"continue":false}`; a pass prints nothing.
- `coach --guard-check` — a provider-neutral check. A stop exits non-zero with a
  reason; a pass exits 0.
- `coach guard on|off|status` — the runtime switch.

The decision is a hybrid OR: `left < timeLeft − m` (pace) **or** `left < floor`
(absolute). Bad data fails open, letting the work through. Configured in
`~/.config/usage-coach/guard.json`, with `m` and `floor` per provider, both
0.10 by default.

## The Discord dashboard (`discord_dash.py`)

A live dashboard that assembles coach's output into a **Components V2** message —
a container per provider with a Unicode gauge bar — and **keeps editing** one
webhook message, so opening the channel always shows the current state. All of it
is native text, so it stays sharp at any width on desktop and mobile. It replaced
a PNG card that was scaled down as a whole the taller it got.

- The layout is a container per provider, its accent colour taken from the level,
  holding a gauge bar per window, **the coaching in full (action and reason)**,
  and the reset time as `<t:..:R>`, which Discord renders as "in 3 hours" in each
  reader's own locale.
- **The bot sessions section** shows the sessions currently working — directory,
  model, context used — from the snapshots the Claude Code statusline leaves in
  `~/.config/usage-coach/sessions/`. That needs a few lines added to your
  statusline script, using `session_id` and `context_window.used_percentage` from
  its input JSON. A session idle for 30 minutes drops off the list.
  - **Record `project_dir`, the folder the session was started in, alongside the
    rest.** With only `current_dir`, the moment a session `cd`s into a subfolder
    the bot row stops matching and the display freezes — measured, not theoretical.
    For example:
    ```jq
    {cwd: (.workspace.current_dir // .cwd // ""),
     project_dir: (.workspace.project_dir // .workspace.current_dir // .cwd // ""),
     model: (.model.display_name // ""),
     used: (.context_window.used_percentage // null), ts: (now | floor)}
    ```
    Matching prefers `project_dir` and falls back to `cwd`, so older snapshots
    keep working.
- A new message is posted — ringing a push notification — **only at the moment** a
  level worsens to 🟡 or 🔴. The ordinary edits are silent.
- No dependencies, the same as coach itself: standard library only.
- A trap: sending components through a webhook needs `?with_components=true` in
  the URL, or the fields are ignored wholesale (`Cannot send an empty message`,
  50006). That applies to both POST and PATCH (the edit).

```bash
python3 discord_dash.py --out c.json        # write the component JSON only, no webhook, for checking
python3 discord_dash.py --mock all --out c.json
python3 discord_dash.py                     # query, assemble, upsert to the webhook, and ping on a worsening
```

Configured in `~/.config/usage-coach/discord.json` as `{"webhook_url": "..."}`,
or through the `DISCORD_WEBHOOK_URL` environment variable. In the same file,
`"coach_args": ["--account", "antigravity:foo@gmail.com"]` pins which account the
dashboard follows, and the card then shows that account's email. A provider you
move between accounts on can go in `"all_accounts"` (`["antigravity"]` by
default): codexbar `--all-accounts` then gives **a bar per registered account**,
while the coaching, the body and the pings follow whichever account has most
room. Set `"all_accounts": []` to turn that off. The state — the message ID and
the previous levels — lives in `discord-state.json`. Run it on a schedule, every
five minutes through a LaunchAgent or similar; it only uses the webhook, so no
bot token and no gateway connection are needed.

## Licence

[MIT](./LICENSE)
