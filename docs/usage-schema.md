# codexbar's usage JSON — the input schema (for the part-one core)

Captured from a real run on 2026-06-25 (the usage-test pane, codexbar 2.1.191).
The originals are in `usage-samples/{claude,codex,gemini}.json`.

## Query commands (one per provider — `--provider all` hangs)
```bash
codexbar usage --provider claude  --format json --pretty
codexbar usage --provider codex --source oauth --format json --pretty   # oauth since 2026-08-22 (the cli source broke on codex 0.149.0's app-server change)
codexbar usage --provider gemini --source api --format json --pretty
```
- ⚠️ `codexbar --provider all` **hangs — confirmed**, and has to be interrupted.
  Fetch each provider separately and combine.
- ⚠️ codex needs `--source oauth` (2026-08-22). The earlier `--source cli` fails
  on codex 0.149.0 with "codex app-server closed stdout". oauth gives the 7d
  window only, with no 5h.

## The shared schema (one array element per provider)
```jsonc
{
  "provider": "claude|codex|gemini",
  "source": "web|codex-cli|api",
  "version": "...",            // absent for gemini
  "usage": {
    "accountEmail": "...",
    "loginMethod": "Claude Pro|plus|Free",
    "primary":   { "usedPercent": N, "windowMinutes": M, "resetsAt": "ISO8601Z", "resetDescription": "..." },
    "secondary": { ... } | null,
    "tertiary":  { ... } | null,
    "extraRateWindows": [ { "id","title","window":{usedPercent,windowMinutes} } ],  // claude only. A Max subscription includes "Fable only" (id=claude-weekly-scoped-fable, seven days), which coach shows as a display-only line and keeps out of the decision. Absent from a standard subscription
    "providerCost": { "limit","used","currencyCode","period" },                      // claude
    "updatedAt": "ISO8601Z"
  },
  "credits": { "remaining": N, "events": [] }   // codex only
}
```
The percentage left is `100 - usedPercent`; the time to reset is `resetsAt - now`.

## ★ The findings that constrain the design

**1. Windows must be classified by `windowMinutes`. Never assume anything from
the primary/secondary position.**

| windowMinutes | meaning |
|---|---|
| 300 | 5 hours |
| 10080 | 7 days (7×1440) |
| 1440 | 1 day (Gemini) |

**2. The window layout differs per provider:**

- **Claude (Pro)**: primary=5h, secondary=7d. The A-to-D cross-reading applies as
  written.
- **Codex (plus)**: primary=5h, secondary=7d, plus `credits.remaining`. The same
  again.
- **Gemini (Free, a separate account)**: primary, secondary and tertiary are
  **all 1440, daily**. It has no notion of 5h or 7d. → **Not excluded. Handled on
  its own "daily window" track.** The reset times are valid (secondary and
  tertiary `resetsAt` are the next day, "Resets in 23h 59m"). Being short, daily
  has **the same use-it-or-lose-it character as 5h**: with a reset close and a lot
  left, the advice is to spend it today — case A, in its daily form. It is about
  spending, not about pace the way 7d is.
  - ⚠️ No title label (claude's extraRateWindows has one). A secondary and
    tertiary sharing a resetsAt are the same daily reset in practice and are
    folded together.

**3. Guarding against invalid data is required:**

- Gemini's **primary only** comes as `resetsAt: "1970-01-01T00:00:00Z"` with
  `usedPercent: 100` — a placeholder, and invalid, so it is skipped. The secondary
  and tertiary are valid and are what the daily figure comes from. Any epoch or
  otherwise nonsensical reset is skipped the same way.

## What this means for the A-to-D recommendation logic

- Normalise the input: each provider's JSON becomes
  `{provider, windows: [{kind: 5h|7d|daily, leftPercent, minutesToReset}]}`,
  mapped by windowMinutes.
- 5h is use-it-or-lose-it; 7d is pace. Gemini's daily sits between the two — a
  judgement for the design.
- Deciding an imminent reset dynamically: if `leftPercent` is more than can be
  spent within `minutesToReset`, advise spending it.
