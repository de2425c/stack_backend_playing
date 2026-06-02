# Polish Pass — Summary

Companion to `EXPERIENCE.md`. Records the concrete timing numbers
chosen, why, what's verified, what isn't, and what needs human
action.

Date: 2026-06-02.

Scope: 37 of the 40 EXPERIENCE.md findings carried code changes.
3 were verified-correct / not-pursued (P2-6, P2-10, P3-8/9/10) and
2 were intentionally skipped (P2-5 screen-size test, P3-4 legacy
view cleanup).

Per-finding status, with commit subjects, is in the **Status** table
at the top of `EXPERIENCE.md`.

---

## Repos touched

- `stack_poker/stackpoker` — most of the iOS work.
- `stack_backend_playing` — server, engine, protocol.
- `stack_poker/openbot` (submodule) — bot client think-time.

Per-finding commits, one finding per commit so each is independently
revertable. Multi-finding commits explicitly enumerate them in the
body. No history rewrites, no force-pushes.

The `stack_poker/openbot_client.py` snapshot in the parent repo was
NOT patched — only the canonical source inside the `openbot`
submodule. Per the original investigation note, the prod bot lives
on the server outside both clones; the snapshot is informational.

---

## Concrete timing numbers and why

### Bot think time (P0-1)

Replaces the fixed 1.0s `asyncio.sleep`. Distribution by (verb, street):

| Verb     | Preflop / Flop | Turn / River |
|----------|----------------|--------------|
| fold     | 0.45–1.10 s    | 0.55–1.40 s  |
| check    | 0.40–0.95 s    | 0.50–1.20 s  |
| call     | 0.65–1.50 s    | 0.80–2.00 s  |
| bet      | 0.90–1.90 s    | 1.00–2.30 s  |
| raise_to | 1.05–2.30 s    | 1.30–3.00 s  |

Plus modifiers:
- Bet/raise sizing scaling: up to +45% for jam-size raises (sized as
  multiple of pot, capped at 3×).
- Big-call scaling: up to +20% for calls >= 2× pot.
- **Late-street tank tail:** 8% chance of 3.0–6.0 s tank on turn/river
  when verb ∈ {call, fold, raise_to} AND facing a bet.
- **Persona modifiers:** "fish" wider variance (×0.7–1.4) + 10%
  snap-click; "TAG"/"nit" ×0.85; "shark"/"lag" 5% chance of 4–7 s
  deep tank on turn/river.

**Clamps:** preflop/flop is hard-clamped to **[0.30, 4.00] s** so we
never blow the server's 5 s early-street auto-fold deadline (the
server timeout in `engine/config.py:bot_early_street_timeout_seconds`).
Turn/river is clamped to **[0.35, 8.00] s** against the 60 s budget.

**Why these numbers:** target a felt cadence where the median fold
reads as "snap" (~0.6 s), the median raise reads as "considered"
(~1.5 s), and ~1-in-12 late-street decisions feels like a genuine
tank. That's what human play actually looks like at low-mid stakes
HU on the major sites. The hard preflop ceiling matters more than
the soft turn ceiling — preflop running long is what gets auto-folded.

### Animation timings (P1-1 et al)

Single source of truth: `PokerAnimationTimings` in
`stack/Features/PokerTable/Model/PokerTableState.swift`.

| Constant | Value | Used by |
|---|---|---|
| `chipPrePause` | 0.30 s | chip task pre-pause |
| `chipStage1`   | 0.60 s | bets → center |
| `chipStage2`   | 0.55 s | center → winner |
| `chipDwell`    | 2.80 s | celebration hold (non-tiny pots) |
| `chipTotal`    | **4.25 s** | derived from above |
| `postChipBreath` | 0.30 s | beat after chip anim |
| `blitzPauseHeroFolded` | 0.30 s | hero folded, no chip anim |
| `blitzPauseHeroWon` | **4.55 s** | chipTotal + breath |
| `blitzPauseShowdown` | **5.75 s** | chipTotal + reading time |
| `blitzPauseOther` | **4.45 s** | chipTotal + small breath |
| `runoutInitialPause` | 1.50 s | suspense before first runout card |
| `runoutBetweenStreets` | 1.50 s | suspense between cards |
| `winnerInfoBaseHold` | 5.00 s | base post-anim hold (showdown) |
| `winnerInfoFoldHold` | 2.00 s | shorter hold for fold/timeout |

The pause-table values are all derived from `chipTotal` so the
celebration is never clipped (the old 3.0 s hero-won pause cut
mid-animation). When `chipTotal` is changed, every dependent value
recomputes.

### Server grace + actor window (P0-3 / P0-7 / P1-3 / P2-7)

- `engine/config.py:action_timeout_grace_ms = 1500` — server-side
  cushion applied to both `is_expired` (incoming action validation)
  and the tick auto-fold. **The client still draws the ring to
  zero at the original `expires_at_ms`** — the grace cushion is
  invisible. Players who tapped fold in time on a flaky network
  get accepted instead of sat out.
- `STATE_DELTA.actor_expires_at_ms` + `actor_window_seconds` are
  now sent server-authoritative for opponent rings. iOS no longer
  fabricates "now + 60_000". Bot turns (5 s preflop/flop) render
  correctly drained instead of looking ~92% empty from frame 1.

### Bot quip lifetime (P1-6)

`PokerTableState.botQuipLifetime = 6.0 s`. Each call records a
fresh UUID token; the scheduled clear no-ops if a newer quip
superseded it. Chose 6 s so the quip lasts through ~1 street of
play but doesn't shadow the next deal.

### Showdown card flip (P1-8)

3D rotation3DEffect, 0.55 s ease-in-out, 0.08 s stagger between
the two cards. Chose 0.55 s as feeling "tactile" rather than
"flicked" (0.30 s read as too snappy) and not slow-mo. Stagger
matches the natural double-tap timing of the deal animation.

---

## What's verified vs unverified

### Verified (compiled/parsed locally)

- All Python files I edited (`handler.py`, `timer.py`, `app.py`,
  `engine/table.py`, `engine/config.py`, `models/messages.py`):
  parsed clean with `python3 -c "import ast; ast.parse(...)"`. No
  syntax / import errors.
- The openbot client (`openbot/src/serving/openbot_client.py`):
  parses clean.

### Unverified (could not exercise here)

- **All iOS Swift changes** — Xcode is not available in this
  environment. I did not build the app. SourceKit-LSP fired
  hundreds of "Cannot find type X" diagnostics throughout — these
  are pre-existing project-context issues (every iOS file in the
  project shows them), not from my changes — so I couldn't even
  rely on diagnostic noise to flag a real bug.
- **No tests were run** on either side.
- **No end-to-end "play a hand" verification** of:
  - The TABLE_SNAPSHOT-before-STATE_DELTA reorder (P1-11). The
    flicker analysis is in the commit body; needs a real hand
    played to confirm.
  - The optimistic-action rollback (P0-6). Needs a real
    ACTION_TIMEOUT or INVALID_ACTION to confirm the panel restores.
  - The 1.5 s server grace landing under real network conditions.
  - The new 6-preset row layout fitting on small screens (iPhone SE).
  - All animation numbers actually FEEL right when played. Numbers
    are based on the read of the existing code + a poker-player's
    mental model, not a play session.

You need to actually play a hand (and ideally a 30-minute session)
to confirm the feel. I expect 1–2 numbers to need tweaking after
that play session, especially `chipDwell` and the bot tank-tail
frequency.

---

## What needs you (the human)

### Required to ship the audio feature (P0-2)

`PokerSoundService` is fully wired but ships in **fallback mode**
(distinct system sounds per cue). To make it audibly real you need
to add the asset files. Asset slot list (drop into
`stack/Resources/Sounds/` or wherever fits the build):

- `poker_your_turn.caf`     — short alert
- `poker_card_deal.caf`     — per-card whoosh
- `poker_card_flip.caf`     — flip from face-down to face-up
- `poker_chip_tap_light.caf` — check
- `poker_chip_tap_medium.caf` — call / bet / raise
- `poker_chip_fold.caf`     — fold (heavier than tap_medium)
- `poker_chip_slide.caf`    — chips moving to pot at street change
- `poker_chip_collect.caf`  — chips to winner
- `poker_win.caf`           — win sting
- `poker_warning.caf`       — sub-5 s warning during action timer

Also add a Settings toggle bound to the existing AppStorage key
`pokerSoundsEnabled` (default `true`). The service already gates
EVERY cue on this flag — one switch covers the lot. Without the
toggle in Settings the user has no obvious way to mute, which
matters for plane / quiet-environment play.

### Subjective calls to validate

- **Bot think-time distribution** (P0-1). Play 50+ hands HU and
  see if bots feel human. Tweak the (lo, hi) ranges and tank-tail
  rate in `_compute_think_time`. The numbers are educated guesses.
- **Chip-celebration tiny-pot threshold** (P2-1). Currently
  `totalPot <= 5 * bigBlind`. May want to be 8 BB or sized by
  fraction of stack instead.
- **winnerInfo fold hold** (P2-3). 2.0 s may feel rushed if the
  user wants to register the flash; 3.0 s might be more readable.
- **Preset row at 6 columns** (P3-1). I haven't tested fit on
  iPhone SE (375 pt width). `minimumScaleFactor + lineLimit(1)`
  should handle it but visually verify.

### Operational

- The server change in P1-11 (TABLE_SNAPSHOT before STATE_DELTA on
  hand_start) is a protocol-ordering change. The bot subprocess
  uses snapshots, not deltas, for state — so it's unaffected. Any
  other clients (web etc.) should be checked.
- The new STATE_DELTA fields `actor_expires_at_ms` /
  `actor_window_seconds` are optional `Optional[int]` — older
  clients without iOS changes will just ignore them. The new iOS
  client falls back to a 60 s default window if the fields are
  missing, so no version-lock-step deployment needed.

---

## New feel-bugs spotted while in the code (didn't fix)

These weren't in the original EXPERIENCE.md but I noticed them
while doing the polish pass. Logging here for the next pass:

1. **`heroPressedFoldThisHand` flag is fragile** — it's set after
   the wire send returns. With the new optimistic-action rollback
   in P0-6, the flag MIGHT not reset on action rejection while
   still being set if the action was retried successfully. Should
   sync with the actionInFlight clear path.

2. **`heroBetExtraOffsetY` / `heroBetScale` parameters on
   PokerTableView** are puzzle-only overrides; their default-zero
   path is fine for bot play but the parameters add API surface
   that's tested in only one mode. Worth a code-cleanup pass.

3. **`Task.sleep(seconds:)` extension I added lives in
   PokerTableState.swift** because it's the file that gained
   `PokerAnimationTimings`. Belongs in a separate `Utilities/`
   file ideally — it's not state-specific.

4. **Sitting-out state** (P3-7) is now tracked on
   `PokerTableState.sittingOutSeats: Set<Int>` but no view reads
   from it yet. The FigmaPlayerSeat seat pill should probably
   render a "SITTING OUT" badge when its seat is in the set. Hook
   exists, UI follow-up needed.

5. **`pendingRunoutCards.append([card])` per-card split (P2-2)**
   removed the multi-card batch case. The runout animator
   currently calls `applyNextRunoutCard()` which removes-first.
   This works but feels slightly mismatched — `pendingRunoutCards`
   is now always a list of single-card arrays. Refactor to
   `[[PokerCard]]` → `[PokerCard]` would simplify, but I left the
   shape alone to avoid a wider edit.

6. **`PokerAnimationTimings.chipDwell` short-circuits to 1.0 s for
   duel-terminal hands** (PokerTableView). That's hand-coded, not
   in the constants table. Either move it into
   `PokerAnimationTimings.duelTerminalDwell = 1.0` or document
   the rationale near the constants.

7. **`ignoreSnapshots`-based race protection (P2-8)** was replaced
   with a `currentTableId` check. There's a brief window during
   `forceLeaveTable` where `currentTableId` is set to nil but a
   pending message may already be parsed. The new gate catches
   it correctly but the timing is very tight. A play test on a
   poor network would reveal any issue.

---

## Status table

For per-finding status and commit subjects, see the **Status
(2026-06-02 pass)** table at the top of `EXPERIENCE.md`.
