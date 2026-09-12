# Part one, the usage coach — the core logic, for implementation

Settled 2026-06-25 by the author. Input schema: `usage-schema.md`. Samples:
`usage-samples/`.

## In one line

It reads codexbar's usage data and says **what size of job to run now, why, and
when it matters**. The output is a recommended action, not a readout — and that
interpret-then-advise layer is the gap no existing tool fills.

## Input (per provider, windows classified by windowMinutes)

Two values are taken from each window:

- `left` = the fraction of usage remaining = (100 − usedPercent) / 100
- `timeLeft` = the fraction of the window still to run = minutesToReset / windowMinutes
  (the absolute `minutesToReset` = resetsAt − now is kept too, for deciding
  whether a reset is imminent)

Kinds of window, by windowMinutes:

- **Burst**: 5 hours (300), daily (1440, Gemini). "It disappears at the reset, so
  leaving it unused is a loss."
- **Pace**: 7 days (10080). "It has to last, so manage the rate."

Invalid-data guard: a resetsAt at the epoch (1970) or otherwise nonsensical means
that window is skipped. (Gemini's primary.)

## The core comparison (the same for every window)

`left` against `timeLeft`:

- `left > timeLeft + m` → you will not get through it in time = **likely to leave
  some, so there is room to spend more**
- `left < timeLeft − m` → **tight, so go carefully**
- between the two → about right

(`m` is the slack margin, 0.10 to begin with. A tuning value.)

That single comparison replaces any "detect an imminent reset" rule — no fixed
"one hour before" threshold is needed.

## Output: a recommended job size (5h and 7d together)

Decided from the top, first match winning:

| # | condition | message | colour |
|---|---|---|---|
| 1 | **the week is tight** (seven.left < seven.timeLeft − m) | "The 7-day limit is tight. Hold the big jobs and run only what you must." Plus, as a middle option, light work that barely touches the week | 🔴 |
| 2 | the week is fine and **5h is roomy** (five.left ≥ T_big) | "This is the moment. Run the big job." | 🟢 |
| 3 | the week is fine, 5h short, **reset imminent** (five.minutesToReset ≤ T_soon) | "Wait a little and the 5 hours reset. A big job straight after fits perfectly." | ⏳ |
| 4 | the week is fine, 5h short, reset far off | "A big job will hit the 5-hour limit and be cut off part way. Keep them small." | 🟡 |
| 5 | anything else (all unremarkable) | "There is room. Carry on as usual." | ⚪ |

**The principle that resolves conflicts**: any job draws down 5h and 7d at once,
so **whichever of the two is tighter sets the ceiling on job size**. That is why a
tight week (#1) outranks a roomy 5h — a coupon going to waste is a shame, but the
weekly budget is the bottleneck.

**The three principles for a message**: ① the recommended job size, ② the reason
("because it will hit the 5-hour limit and be cut off"), ③ the timing ("in a
little while"). The reason and the timing are what distinguish this feature —
drop them and it is just another readout.

## Tuning (initial values, to be adjusted to how you work)

- `m`, the pace margin = 0.10
- `T_big`, the 5h floor below which a big job is not worth starting = 0.40,
  assuming one big job consumes about 30% of the 5h, plus a buffer. **Worth
  recalibrating against what one of your own big jobs actually costs.**
- `T_soon`, the minutes within which a reset is "imminent" = 30 (or, dynamically,
  less than a big job is expected to take)

## Per provider

- **Claude/Codex (5h+7d)**: exactly the logic above. These are the main ones.
- **Gemini (three daily windows, a separate account)**: daily is a burst window
  and takes the "5h" place in #2 through #4. With no 7-day pace, #1 is skipped.
  A secondary and tertiary sharing a reset are folded into one. The primary
  (epoch) is guarded against.
- The output is per provider, plus an optional overall line.

## Where track B connects (part two)

The agent-loop rate guard consumes this classification, so instead of an
unconditional stop it can have a policy per case #1 to #5 — refuse or shrink a big
job at #1, wait for the reset at #3, ask the user at the boundary. mat picks this
up too.
