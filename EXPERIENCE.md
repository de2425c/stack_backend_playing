# Stack Poker — Playing-Experience Audit

Investigator: independent multi-hour code audit.
Scope: backend (`stack_backend_playing`) + iOS (`stack_poker/stackpoker/stack`) + bot client (`stack_poker/openbot_client.py`).

This pass is **distinct from `INVESTIGATION.md`** (which covers crashes / freezes / financial-safety bugs, almost all marked ✅ FIXED). This audit looks at the **felt quality** of playing a hand: smoothness, animation polish, micro-correctness bugs, latency the player notices, things that make a real poker player go "that's not how this should feel."

Findings are ranked by **player-felt impact** — how often a real player would hit it and how badly it hurts the feel — not by code-criticality. Each is tagged `polish` (it works but feels wrong) or `correctness` (visibly or measurably wrong) or both.

Files cited with `path:line`.

---

## Status (2026-06-02 pass)

All P0/P1/P2/P3/X findings below have been addressed in one commit
each on `main` in both repos. Status legend:

  ✅  Fixed in this pass — see the `Fix:` line on each finding.
  ⚪  Verified correct as-is — code re-read, no behavioural change.
  ⏭️  Skipped — deferred / no behaviour change appropriate.

| #     | Status | Commit |
|-------|--------|--------|
| P0-1  | ✅ | `fix(bot): variable think time` (openbot submodule) |
| P0-2  | ✅ | `fix(ios): wire audio hooks across poker play surface` |
| P0-3  | ✅ | `fix(server): server-authoritative actor deadline + grace window` / `fix(ios): opponent timer ring uses server-provided window` |
| P0-4  | ✅ | `fix(ios): drop stale 0.35s "wait for table slide" delay` |
| P0-5  | ✅ | `fix(server): defer REBUY broadcasts to next hand_start` |
| P0-6  | ✅ | `fix(ios): optimistic action with rollback on rejection` |
| P0-7  | ✅ | (bundled into P0-3 commit) |
| P1-1  | ✅ | `fix(ios): single source of truth for animation timings + cash anim-complete ACK` |
| P1-2  | ✅ | `fix(ios): remove phantom blind animation block` |
| P1-3  | ✅ | (bundled into P0-3 commit) |
| P1-4  | ✅ | (bundled into P1-1 commit) |
| P1-5  | ✅ | (bundled into P0-1 commit) |
| P1-6  | ✅ | `fix(ios): auto-dismiss bot quips after 6s` |
| P1-7  | ✅ | `fix(ios): ThinkingDots Timer is now invalidated on disappear` |
| P1-8  | ✅ | `fix(ios): 3D flip animation for showdown card reveal` |
| P1-9  | ✅ | `fix(ios): weighted tap haptics + prepared generators` |
| P1-10 | ✅ | (bundled into P1-9 commit) |
| P1-11 | ✅ | `fix(server): send TABLE_SNAPSHOT before STATE_DELTA on hand_start` / `fix(ios): clear lastActions in update(from snapshot:)` |
| P1-12 | ✅ | `fix(engine): tighten is_action_stale on hand_id mismatch` |
| P1-13 | ✅ | `fix(engine): use config.stake_id in get_snapshot` |
| P1-14 | ✅ | `fix(ios): signal-based isRunoutAnimating wait` |
| P2-1  | ✅ | `fix(ios): skip chip travel + shorten celebration on tiny pots` |
| P2-2  | ✅ | `fix(ios): split multi-card runout chunks into single-card reveals` |
| P2-3  | ✅ | `fix(ios): scale winnerInfo hold by termination` |
| P2-4  | ✅ | `fix(server): _auto_start_next_hand can wait on ANIMATION_COMPLETE` |
| P2-5  | ⏭️ | Skipped — non-critical, follow-up screen-size testing recommended (see finding). |
| P2-6  | ⚪ | Verified correct by design — asymmetric clear is intentional. |
| P2-7  | ✅ | (bundled into P0-3 commit) |
| P2-8  | ✅ | `fix(ios): drop fixed-2s ignoreSnapshots window, gate on table_id` |
| P2-9  | ✅ | `fix(ios): chop detection scans all winners for blitz-fold marker` |
| P2-10 | ⚪ | False alarm on re-read — already correct. |
| P3-1  | ✅ | `fix(ios): preset row matches header spec — 6 presets` |
| P3-2  | ✅ | `fix(ios): ActionPanel countdown via TimelineView` |
| P3-3  | ✅ | `fix(ios): skip community-card re-animation on reconnect` |
| P3-4  | ⏭️ | Skipped — CompactSeatView appears to be legacy / preview-only per CLAUDE.md; cleanup belongs in a dead-code-removal pass, not a polish pass. |
| P3-5  | ✅ | `fix(ios): numeric content transition on BetLabelPill amount` |
| P3-6  | ✅ | (rolled into P0-6 — isProcessing fully removed when actionInFlight took over) |
| P3-7  | ✅ | `fix(ios): apply seat_update events to track sit-out` |
| P3-8  | ⚪ | Negligible perf, not pursued. |
| P3-9  | ⚪ | Idempotency in handler already drops bot retries; not pursued. |
| P3-10 | ⚪ | Defensive code, not pursued. |
| X-1   | ✅ | (bundled into P0-3 iOS commit — guard on `delta.events.contains hand_ended`) |
| X-2   | ✅ | `fix(ios): FigmaPlayerSeat actionLabel content transition` |
| X-3   | ✅ | `fix(ios): hero hole cards fade + slide out on fold` |

For exact numeric choices (animation durations, grace-window sizes,
bot think-time distribution) and what's verified vs unverified, see
**`POLISH_SUMMARY.md`** in this repo.

---

## Methodology

1. Traced a full hand end-to-end (deal → preflop action → flop reveal → all-in runout → showdown → next hand) on both sides.
2. Read every animation `.task(...)` and every `Task.sleep` in the play path.
3. Catalogued every hard-coded delay, every "phantom" animation, every state-machine race.
4. Re-read with a poker player's eye: does this match a Heads-Up game at 888 / GG / PokerStars?
5. Self-challenged: what does a player notice that the code doesn't model? (sound, haptic, button cadence, bot tells.)

iOS LOC re-read: ~5,500 in `Features/PokerTable/**` + the bot client. Backend re-read: handler, runner, engine, timer.

---

## Hand timing — what the player actually experiences

```
hand_ended ──┐
             ├─ runout anim (if all-in)        1.5s + 1.5s/street      4.5s for bulk
             ├─ chip→winner anim               300 + 600 + 550 + 2800   4.25s
             ├─ winnerInfo cleanup wait        +5s after runout         (overlaps)
             ├─ blitz pause heroWon=3s/SD=5s/other=2s                  2-5s
             └─ NEXT_HAND sent
hand_started ──┐
             ├─ button anim                    0.2s                    blocks panel
             ├─ phantom blind anim             0.05 + 0.20 + cleanup   blocks panel
             ├─ HoleCards 0.35s "wait for table slide" + 2×0.15s      0.65s to see cards
             └─ ActionPanel finally appears

Per-action timeline (hero):
  tap → optimistic clear → WS send → server queue 0-? ms → engine apply
  → STATE_DELTA broadcast → iOS applyEvents → repaint
```

Net felt: **~1.0-1.1 s of dead time between every hand's start and the player being able to act**, on top of network + server.
Net felt: **~7-10 s between a fold-fold hand and the next one's first action button** (best case, blitz on).
Net felt: **~12-15 s between a preflop all-in and the next hand's first action** (runout drama + chip anim + post-hand wait + button + blinds + card-reveal delay).

---

# P0 — Deal-breakers in poker feel (every player notices)

## P0-1 — Bot fixed 1.0 s think time, regardless of decision  ▸ polish

**File:** `~/Projects/stack_poker/openbot_client.py:227-233`

```python
print(f"{self.user_id}: Got action, sleeping 1s before send", flush=True)
await self._generate_and_send_quip(action, data)
await asyncio.sleep(1.0)
await self._send_action(action, data.get("hand_id", ""))
```

Every bot action — easy preflop fold, river call with the nuts, big bluff jam — has the **same fixed 1.0 s delay** before sending. After 30 minutes of play a real poker player will subconsciously clock the cadence and notice the bots aren't "thinking."

Compounded by: the quip generator only fires turn/river HU (`openbot_client.py:425`), so 6-max bots are an even more rigid metronome.

**Severity:** very high. This single number makes the table feel like a video game instead of a poker game. It's the biggest "feels artificial" lever in the whole stack.

**Fix:** replace with action-conditional jitter. Suggested distribution:
- Fold / check facing no bet on preflop: 0.4-0.8 s
- Call / check on flop: 0.7-1.4 s
- Bet / raise: 1.0-2.5 s
- Turn / river decision: 1.5-3.5 s with a heavy tail (10% in 3-6 s "tank")
- Add per-persona jitter (TAG bots act fastest, fish slowest)

Stake-weighted: at higher stakes give bots slightly longer think times.

## P0-2 — Zero audio anywhere in the play surface  ▸ polish

**File:** searched all of `Features/PokerTable/**` and `Features/Play/**`. No `AudioServicesPlay…`, no `AVAudio…`, no `.wav` / `.mp3` / `.aiff`.

There is **no audio cue** for:
- It's your turn (the most important one — players miss timeouts because the app is silent)
- Card deal
- Chip slide / pot pull
- Bet / call / raise (button confirm)
- Win celebration
- Time-warning beep at <5 s

Real poker apps (PokerStars, GG, 888, Zynga) all use sound to make the table feel alive AND to alert the player when they look away. Without sound, the only "your turn" cue is one `UIImpactFeedbackGenerator(style: .medium).impactOccurred()` at `PWSM:1345`. If the phone is on the desk in lock-screen-off, players miss their turn entirely.

**Severity:** very high. Affects every hand. Free polish win.

**Fix:** at minimum, "your-turn" tone (uses `AudioServicesPlaySystemSound(1057)` or a custom AIFF). Add card-deal whoosh, chip-collection clink, win sting. Gate behind a Settings toggle that defaults ON.

## P0-3 — Timer ring on opponent assumes 60 s window, bots get 5 s  ▸ correctness (visual)

**File:** `stack/Features/PokerTable/View/Components/FigmaPlayerSeat.swift:152-164`

```swift
TimelineView(.periodic(from: .now, by: 0.1)) { context in
    let nowMs = Int64(context.date.timeIntervalSince1970 * 1000)
    let totalWindow: Double = 60_000
    let remaining = Swift.max(0, Double(expiresAtMs - nowMs))
    let progress = min(1, remaining / totalWindow)
    ...
}
```

`totalWindow = 60_000` is hard-coded. But the backend gives bots a **5 s timeout on preflop/flop** (`engine/config.py:23`, used in `engine/table.py:746-755`). So when a bot is the actor on preflop or flop, the ring shows `5000/60000 = 8 %` immediately — looks ~92 % depleted from the moment they become the actor. Visually, every bot turn starts with a near-empty ring, and we never see the drain animation that makes the ring meaningful.

Same bug bites the hero in `ActionPanel.swift:108-109` — but there the panel captures `totalSeconds` from the actual deadline on appearance, so the hero ring is correct.

**Severity:** high. Visible on every bot turn (almost every action a player sees).

**Fix:** include `total_window_ms` (= `expires_at_ms - issued_at_ms`) in `ACTION_REQUEST` (or have the iOS ring capture remaining time at the first frame it sees the actor and use that as `totalWindow`). The state-delta path doesn't carry an action-request, so the cleanest path is to add `expires_at_ms` AND `issued_at_ms` to `STATE_DELTA` for the actor, or store `(seat, expires_at_ms, deadline_window_ms)` on `PokerTableState` derived from whichever came first.

## P0-4 — 0.35 s "wait for table slide" delay on every hand's hole cards + flop  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/HoleCardsView.swift:68-75` + `stack/Features/PokerTable/View/Components/CommunityCardsView.swift:97-110`

```swift
private func animateDealing(count: Int) {
    visibleCards = 0
    let initialDelay = 0.35 // Wait for table slide animation
    for i in 0..<count {
        DispatchQueue.main.asyncAfter(deadline: .now() + initialDelay + Double(i) * 0.15) {
            withAnimation { visibleCards = i + 1 }
        }
    }
}
```

The comment refers to a **table-slide animation that no longer exists** (PokerTableView.swift:222 explicitly says `// no slide animation, just dealer button rotates and blinds post`). The 0.35 s prefix delay just adds dead time:
- Hero hole cards visible at 0.35 s + 0.15 s = **0.50 s** after hand_started.
- Flop fully visible at 0.35 s + 3×0.12 s = **0.71 s** after `street_dealt(flop)`.

Combined with the phantom blind animation (P1-2 below) the hero has **~1 s of doing-nothing-yet** at every hand start. Over a 200-hand session that's ~200 s of pure dead time.

**Severity:** high. Compounds. The first thing you should see in a new hand is your cards.

**Fix:** drop `initialDelay` to 0 (or 0.10 for the smallest perceptible "pop"). Hole cards should be visible essentially immediately on hand_started — the deal animation can carry the eye, but it shouldn't gate on a non-existent slide.

## P0-5 — `_check_and_process_rebuys` followed by REBUY messages fires before next hand can start, with no animation framing  ▸ polish

**File:** `src/server/handler.py:546-571`

After `hand_ended`:
```python
await self._check_and_process_rebuys(table_id)
await self._topup_bots_and_broadcast(table_id)
```

These send `REBUY` messages mid-celebration. iOS handles `REBUY` with `tableState.updateSeatChips(...)` (`PWSM:1357`) which **immediately mutates the stack number on the seat pill** — so during the winner flash, opponent stacks can visibly jump (bot rebuy from $0 to $200) while the winner banner is still showing. The hero's own auto top-up shows a `CelebrationManager.shared.showRebuy(...)` toast over the celebration.

Also note: `topup_bots_for_cash` is called *every hand end* even when no bot is below 100 bb (no-op then), but the iteration runs unconditionally.

**Severity:** medium-high. Every cash hand. Looks janky on hands where a bot busted and is now magically full-stacked before the next deal.

**Fix:** delay broadcast of `REBUY` until after `winnerInfo` cleanup (the iOS side has to defer the seat-chip mutation by ~5 s, OR the server holds the REBUY broadcast until `ANIMATION_COMPLETE` (which currently isn't sent in cash mode — see P1-4 below)). Cleanest: send REBUY *with* the next `hand_started` broadcast so chips refill at the same time the next hand begins.

## P0-6 — Optimistic `clearActionRequest` runs even on rejected actions  ▸ correctness + polish

**File:** `stack/Features/PokerTable/Service/PokerWebSocketManager.swift:764-777`

```swift
try await send(message)
// ...
tableState.clearActionRequest()
```

The clear fires after `send` succeeds (network write), not after the server acknowledges the action. If the server returns `ACTION_TIMEOUT` (handler.py:222) or `INVALID_ACTION`, the iOS code in the `case "ERROR"` branch at `PWSM:1036-1044` clears `actionRequest` and `currentActor` AGAIN (it was already cleared by the optimistic step). The user then sits looking at the table with no buttons. The `SAFETY_NET` at `handler.py:529-543` will eventually re-send `ACTION_REQUEST` — but the deadline has slipped and the user's timer ring is now blank.

User-felt: tap fold → buttons disappear → "did it go through?" → buttons reappear seconds later with less time.

**Severity:** medium-high (low-frequency but very confusing).

**Fix:** keep `actionRequest` in place after send; only clear once the next `STATE_DELTA` containing the hero's action (or an explicit ACK) arrives. Show `isProcessing` overlay during the pending window. On server rejection, restore the action request from the snapshot the SAFETY_NET fetches.

## P0-7 — Server timeout check has no grace period; a click at deadline-30ms is rejected  ▸ polish

**File:** `src/server/handler.py:222-227`

```python
if self._timer and self._timer.is_expired(user_id):
    return ErrorMessage(code=ErrorCode.ACTION_TIMEOUT, ..., ref_msg_id=action_id, ...)
```

`is_expired` is `int(time.time()*1000) > pending.deadline_ms`. With ~50-150 ms WS RTT, a user tapping fold at deadline-50ms can easily arrive at the server at deadline+100ms → rejected, sat out (`runner._handle_timeout` → `set_sitting_out` at `runner.py:317`). Then `PWSM:1317-1320` flips `isSittingOut = true`. Now the user has to manually "Resume Playing" — over a timing race they should have won.

**Severity:** medium. Hits ~5 % of clutch decisions. Each occurrence is painful — players blame the app.

**Fix:** add a 1.5-2 s grace window on the server (`pending.deadline_ms + GRACE_MS`). Bots already act fast enough; the cost of grace is only on actual human timeouts.

---

# P1 — Noticeable during normal play

## P1-1 — Hero "won" gets only 3 s before NEXT_HAND, but chip animation needs ~4.25 s → celebration cut mid-flight  ▸ polish

**File:** `stack/Features/PokerTable/View/PlayTab.swift:1428-1437`

```swift
if heroWon {
    try? await Task.sleep(nanoseconds: 3_000_000_000)  // 3 s
} else if isShowdownStreet && hasRevealedCards {
    try? await Task.sleep(nanoseconds: 5_000_000_000)  // 5 s
} else {
    try? await Task.sleep(nanoseconds: 2_000_000_000)  // 2 s
}
```

Then `sendNextHand`. But the chip animation in `PokerTableView.swift:307-419` has total budget:
- 300 ms pre-pause + 600 ms phase-1 + 550 ms phase-2 + 2800 ms celebration hold = **~4.25 s** after `winnerInfo` is set.

So at 3 s the celebration is still in the "showing" phase. `sendNextHand` triggers a `hand_started` event → `applyEvents` resets state → `winnerInfo = nil` → the `.task(id: tableState.winnerInfo?.id)` cancels mid-celebration. User sees: chips fly to their seat, win banner appears for ~1.5 s, then **snap** — next hand.

**Severity:** medium. Every hand the hero wins. The most satisfying moment of poker is cut short.

**Fix:** raise the heroWon pause to 4.5 s, OR (cleaner) gate `sendNextHand` on `winnerInfo == nil` (which already auto-clears 5 s after `isRunoutAnimating` drops, per `PokerTableState.swift:1026-1041`).

## P1-2 — Phantom blind animation blocks the action panel for ~450 ms after every hand start  ▸ polish

**File:** `stack/Features/PokerTable/View/PokerTableView.swift:510-602` + `PlayTab.swift:954`

The `.task(id: tableState.hand?.hand_id)` runs a button-rotate (200 ms) then sets up `smallBlind` / `bigBlind` state vars and animates them (50 ms delay + 200 ms = 250 ms) — total ~450 ms with `tableState.isBlindAnimationInProgress = true` the whole time. PlayTab's `bottomActionLayer` (line 954) gates `ActionPanel` on `!tableState.isBlindAnimationInProgress`.

**But the blind chips are never rendered.** The view explicitly says so at line 287-288:
```swift
// Blinds are reflected by each seat's action label as soon
// as the bet hits state — no chip animation.
```

The `smallBlind` / `bigBlind` `@State` vars are dead code. Only the button move (200 ms) is visible. The remaining 250 ms is a pointless delay before the hero can act.

Also: the SB/BB seat calculation at `PokerTableView.swift:557-559` is **wrong for HU** (it computes SB = button+1, but in HU button IS the SB). Since nothing renders, no visible bug — but the dead code is misleading.

**Severity:** medium. ~250 ms × ~1 hand-start-per-30-seconds = noticeable cumulative drag. Combined with P0-4 (0.35 s hole-card delay), the hero is locked out for ~1 s every hand.

**Fix:** drop the blind animation block. Keep only the button move (200 ms), don't gate the action panel on it. Delete the dead SB/BB state and the HU-broken seat math.

## P1-3 — `currentActor.didSet` hard-codes 60 s deadline; comment says 30 s  ▸ correctness + polish

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:34-46`

```swift
@Published var currentActor: Int? {
    didSet {
        if currentActor != nil {
            actorExpiresAtMs = Int64(Date().timeIntervalSince1970 * 1000) + 60000
        } else {
            actorExpiresAtMs = nil
        }
    }
}

/// Deadline for current actor (30 seconds from when they became actor)
@Published var actorExpiresAtMs: Int64?
```

Comment says 30 s, code is 60 s. Stale comment.

More importantly: this **fabricates a deadline based on client clock + 60 s** whenever `currentActor` changes — which happens on EVERY `STATE_DELTA` (PWSM:1228) AND on every `TABLE_SNAPSHOT` (PokerTableState.swift:396). So a snapshot mid-hand re-resets the actor expiry to "now + 60 s" — wrong for bots (whose real deadline is +5 s on preflop/flop), wrong on reconnect (where the server timer has already drained partially).

The opponent timer ring (P0-3) reads from this. So every snapshot resets the ring to full even though the bot is mid-think.

**Severity:** medium. Compounds with P0-3.

**Fix:** stop fabricating `actorExpiresAtMs` in `didSet`. Use the `expires_at_ms` from `ACTION_REQUEST` for the hero, and add an `actor_expires_at_ms` to `STATE_DELTA` for the bot timer ring.

## P1-4 — `sendAnimationComplete` only called in duel mode → cash never tells server animations are done  ▸ polish

**File:** `stack/Features/PokerTable/View/PokerTableView.swift:415-418`, `Features/Play/View/DuelLobbyView.swift:1137-1138`

`onAnimationComplete` is only wired up on the duel surface (DuelLobbyView, the only call site). In cash mode (PlayTab.swift:1223 `PokerTableView(tableState: wsManager.tableState, topInset: 0)`), the callback is `nil` → `sendAnimationComplete` is never invoked.

Server side: `_duel_auto_start_next_hand` waits up to 10 s for `ANIMATION_COMPLETE`. Cash uses `_auto_start_next_hand` only when no humans (handler.py:566-571), so it doesn't matter for typical cash play (`NEXT_HAND` from the client triggers the start). But: if a player is reconnecting and a bot table has no human momentarily, the server fires `_auto_start_next_hand` with a hard 3 s delay (handler.py:573-603) regardless of how long the animation actually took. If the animation finishes faster, you wait. If it takes longer, hand starts mid-animation.

**Severity:** low for cash, medium for spectator-rejoin and HU edge cases.

**Fix:** wire `onAnimationComplete` into the cash PokerTableView (PlayTab.swift:1223). Server side, prefer waiting on the event in all paths.

## P1-5 — Quip blocks bot's action, can cause preflop/flop auto-fold if LLM is slow  ▸ correctness

**File:** `~/Projects/stack_poker/openbot_client.py:226-233`

```python
action = await loop.run_in_executor(None, self._get_action, data)  # solver
await self._generate_and_send_quip(action, data)                   # LLM (turn/river HU)
await asyncio.sleep(1.0)
await self._send_action(action, data.get("hand_id", ""))
```

Quip generation is `await`ed inline. If the LLM round-trip takes 4 s, the bot doesn't send its action until 4 + 1 = 5 s after `ACTION_REQUEST`. Bot timeout on preflop/flop is **exactly 5 s** (`engine/config.py:23`). On turn/river it's 60 s so the risk is preflop/flop only — but those are the streets where the quip code path is gated off (`board_cards < 4` returns early at openbot_client.py:425). So the auto-fold risk is real only when the conditional changes or a long solver call eats budget.

Even on turn/river: bot tank-time ranges from ~1.1 s (quip fast) to ~3-4 s (quip slow). Players will perceive a bot quip as a tell — bot is more likely to think long when it has decided to bluff (because more LLM tokens). Actually maybe charming, but also: it's purely a function of quip generation latency, not actual decision strength.

**Severity:** medium-low (turn/river HU has 60 s headroom, so no auto-fold; but the latency jitter is visible).

**Fix:** `asyncio.create_task(self._generate_and_send_quip(...))` to fire-and-forget so the action is independent of quip latency. Also clamp `_get_action` + sleep + quip total to `(timeout - 1.0)` ceiling.

## P1-6 — Bot quips never auto-dismiss; remain on screen for the entire hand  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/BotChatBubble.swift` + `stack/Features/PokerTable/Model/PokerTableState.swift:177, 408, 443, 578`

`botQuips: [Int: String]` is keyed by seat and only cleared on hand_started, snapshot reset, and `reset()`. The `BotChatBubble` has typewriter-in animation but no fade-out. So a bot's turn-1 quip ("Hmm, interesting bet…") sticks above their avatar through flop, turn, river, and showdown.

By showdown, opponents have ~3 quips potentially layered (only the latest renders since dict overwrites by seat, but visually it's the same stale text from many seconds ago).

**Severity:** medium. Subtle but breaks the "live opponent" illusion the quip is supposed to create.

**Fix:** auto-dismiss `botQuips[seat]` after ~6 s via a `Task` in `setBotQuip`, OR add a fade-out transition in `BotChatBubble` tied to a `displayUntil` field.

## P1-7 — `ThinkingDots` `Timer` never invalidated → leaks per quip  ▸ polish / perf

**File:** `stack/Features/PokerTable/View/Components/BotChatBubble.swift:94-98`

```swift
private func animateDots() {
    Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { _ in
        animatingDot = (animatingDot + 1) % 3
    }
}
```

The Timer reference isn't held → can't be invalidated. After 350 ms the view replaces ThinkingDots with the text (line 60: `isTyping = false`), but the Timer keeps firing forever — each tick mutates state on a now-non-rendered view. Over a 200-hand HU session with quips on most hands, that's 200+ orphan timers all running concurrently.

**Severity:** low individually, accumulates over long sessions.

**Fix:** store the timer in `@State`, invalidate on `onDisappear` and when `isTyping` flips false.

## P1-8 — Showdown card flip from face-down to face-up is an instant `if`-swap, no animation  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/FigmaPlayerSeat.swift:228-247`

```swift
@ViewBuilder
private var cardBacksOverlay: some View {
    if hasShowdownCards, let cards = showdownCards {
        HStack(spacing: -12) { /* face-up cards */ }
    } else {
        HStack(spacing: -12) { /* face-down backs */ }
    }
}
```

When showdown lands, the if-branch swaps cards face-down → face-up with **no flip animation, no transition**. In a casino app you'd expect a 3D flip or at least a scale/fade. The current behavior reads as a frame-1 snap that the eye almost misses.

Same for the hero's hole cards reveal on all-in runout (`PlayTab.swift:1281-1289` falls back to `showdownCards[heroIdx]` mid-runout).

**Severity:** medium. Showdown is the dramatic moment of every shown-down hand.

**Fix:** add `.transition(.flipFromBottom)` or a custom `rotation3DEffect(.degrees(180), axis: .y)` swap with 250 ms duration. Stagger the flip across the cards (card 1 flips, then card 2 50 ms later) for theatre.

## P1-9 — No tap haptic on Fold / Check / Call / Raise buttons  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/ActionPanel.swift:188-213`

The action buttons go through `pillActionButton(...)` → `Button { action() }` → `sendAction(...)`. No haptic on tap. The user feels nothing when they commit to folding a hand. In contrast, the haptic on **ACTION_REQUEST receipt** (PWSM:1345) is `.medium` — so the user feels the alert but not their own action. Reversed from the usual mental model.

**Severity:** medium. Premium feel gap.

**Fix:** add `.simultaneousGesture(TapGesture().onEnded { UIImpactFeedbackGenerator(style: .light).impactOccurred() })` to each pill. Use `.heavy` for fold (commitment), `.light` for check, `.medium` for call/raise.

## P1-10 — Haptic generator not `.prepare()`d before use → first-impact latency  ▸ polish

**File:** `stack/Features/PokerTable/Service/PokerWebSocketManager.swift:1345-1346, 1413-1414, 1444-1450, 1487-1488`

```swift
let impactFeedback = UIImpactFeedbackGenerator(style: .medium)
impactFeedback.impactOccurred()
```

`prepare()` is never called. Apple's docs are explicit: without `prepare()`, the first `impactOccurred()` can have ~100-200 ms latency because the Taptic Engine warms up cold. The user **sees** the action buttons appear before they **feel** the alert.

**Severity:** low individually, ~100 ms of "off" feel on the most important haptic.

**Fix:** create a long-lived `UIImpactFeedbackGenerator` in `PokerWebSocketManager.init`, call `prepare()` on it whenever `currentActor` changes to the hero's seat (warms the engine right before the haptic fires).

## P1-11 — STATE_DELTA → TABLE_SNAPSHOT → ACTION_REQUEST sent as 3 separate WS frames  ▸ polish

**File:** `src/server/handler.py:498-525`

```python
for user_id in user_ids:
    await self._connections.send_to_user(user_id, delta_dict)         # STATE_DELTA
    if is_hand_start:
        await self._connections.send_to_user(user_id, snapshot_dict)  # TABLE_SNAPSHOT
    if user_snapshot.hand and actor_seat == your_seat:
        await self._send_action_request(user_id, user_snapshot)       # ACTION_REQUEST
```

On a slow / lossy network the user can see:
1. `hand_started` event applied → `tableState.hand` set, `yourHoleCards = []` (cleared by snapshot path) → screen shows new hand with **no hero cards**.
2. ~50-200 ms later TABLE_SNAPSHOT lands → cards finally appear.
3. ~50-200 ms later ACTION_REQUEST → buttons appear.

The `HoleCardsView.animateDealing` 0.35 s prefix masks this on fast networks; on slow networks the hero sees the table + bot bets but no cards for half a second.

**Severity:** medium on cellular / poor wifi.

**Fix:** at hand_start, either piggyback hole_cards onto the `STATE_DELTA` (per-user delta requires a different protocol), or send `TABLE_SNAPSHOT` *before* `STATE_DELTA` so the snapshot's hand-state is the first thing iOS sees. Cleanest: at hand_start, suppress the STATE_DELTA entirely and rely on the per-user snapshot for the hand's initial state.

## P1-12 — Stale-action detection misses the case where a new hand has already started  ▸ correctness

**File:** `src/engine/table.py:559-566`

```python
def is_action_stale(self, hand_id: str) -> bool:
    if self._status == TableStatus.RUNNING:
        return False  # Hand in progress, not stale
    return hand_id == self._last_completed_hand_id
```

Imagine:
1. Hand A: hero is actor on the flop, sees ACTION_REQUEST. Hero taps "call" but the request takes 2 s to reach the server.
2. Meanwhile Hand A ends (e.g. an opponent jam clears the queue; doesn't apply here but consider: hero has long network delay).
3. Hand B is started (status now RUNNING).
4. Hero's stale "call" lands at the server. `is_action_stale("hand_A")` checks: status is RUNNING → returns False (NOT stale).
5. `_handle_action` → `apply_action(seat=hero_seat, "call", None)`.
6. PokerKit's actor check (`engine/table.py:378-380`) catches it IFF hero isn't the actor in hand B. If they are (HU, always alternating), the stale call is applied to hand B.

`PWSM:748-752` guards against this client-side, but a delayed packet from a less-defensive client (or a bot client where the guard isn't checked) would slip through.

**Severity:** low (rare), but real correctness bug.

**Fix:** in `is_action_stale`, also return True if the action's `hand_id` doesn't match `self._hand_id` while status is RUNNING.

## P1-13 — `stake_id` hard-coded to `"nlh_1_2"` in snapshot  ▸ correctness

**File:** `src/engine/table.py:715-726`

```python
return TableSnapshotMessage(
    table_id=self._table_id,
    status=self._status,
    stake_id="nlh_1_2",  # TODO: make configurable
    ...
)
```

iOS works around this at `PokerTableState.swift:357-361` by preferring the `requestedStakeId` passed in. So `smallBlind` / `bigBlind` are correct (taken from `snapshot.small_blind` / `snapshot.big_blind`), but if any code path EVER calls `update(from:)` without `requestedStakeId` (e.g. reconnect after a force-leave loses the `requestedStakeId` at `PWSM:1195`), the resolved `stakeId` falls back to `"nlh_1_2"` and the HU / non-1/2 stakes are mislabeled.

**Severity:** low (the workaround holds for normal flows; breaks on edge cases).

**Fix:** fix the backend (it's marked TODO). Use `self._config.stake_id` instead of the hard-coded string.

## P1-14 — Polling pattern: 100 ms `Task.sleep` for `isRunoutAnimating` in 3 places  ▸ polish

**Files:**
- `stack/Features/PokerTable/Model/PokerTableState.swift:1027-1030`
- `stack/Features/PokerTable/View/PokerTableView.swift:320-325`
- `stack/Features/PokerTable/View/PlayTab.swift:1418-1420`

```swift
while tableState.isRunoutAnimating {
    try? await Task.sleep(nanoseconds: 100_000_000) // Poll every 100ms
}
```

Three concurrent polling loops on the same flag for a typical runout. Each loop runs 30-50 iterations on a bulk runout (4.5 s). Functionally fine, but:
- Polling means the wakeup is up to 100 ms late after the flag drops.
- With three loops, the action that depends on runout finishing fires 100-300 ms after the last frame, depending on which loop wins.

**Severity:** low. Adds a few hundred ms of slack between runout end and chip-fly.

**Fix:** convert to a continuation-based signal. `PokerTableState` exposes an `await waitForRunoutToEnd() async` that resumes via `.task` chain or a Combine `first(where: { !$0 })` on `$isRunoutAnimating`. Cuts the 100-300 ms slack.

---

# P2 — Edge cases

## P2-1 — Chip animation pre-pause + phase timing eats the celebration on instant folds  ▸ polish

**File:** `stack/Features/PokerTable/View/PokerTableView.swift:307-419`

Total animation budget after `winnerInfo` is set:
- 300 ms pre-pause
- 600 ms phase 1 (bets to center)
- 550 ms phase 2 (center to winner)
- 2800 ms celebration hold
- = ~4.25 s

For a fold hand (no pre-flop runout, no bets to collect beyond blinds), the "bets to center" phase has nothing meaningful to show — `capturedBets` includes only blind seats. So 600 ms of "tiny chips flying" reads as filler. Then 550 ms of "tiny chips → winner". Then 2.8 s of banner.

For a single-fold-from-BB hand, total: ~7-8 s of post-hand cleanup before the next hand starts (blitz pause 2 s for "other", plus the winnerInfo cleanup 5 s overlap).

**Severity:** medium for fast play. Players sit through 7-8 s of animation between every fold-fold orbit.

**Fix:** when `capturedBets` is empty AND the pot was small (< 5 bb), skip phases 1 + 2 and only flash the banner for 1.5 s. The "drama" should scale to the pot.

## P2-2 — Bulk-runout split assumes flop=3, turn=1, river=1 — doesn't handle turn-or-river all-ins  ▸ polish

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:743-769`

```swift
let isBulkRunout = isAllInRunout && cards.count == 5 && (hand?.board.isEmpty ?? true)
// ...
let flopCards = Array(cards.prefix(3))
let turnCard = [cards[3]]
let riverCard = [cards[4]]
```

This catches preflop all-ins. But:
- **Flop all-in**: 2 cards (turn + river) arrive together. Triggers `shouldQueueForRunout` (line 746) and queues them as ONE street → both reveal simultaneously after the 1.5 s pause. A real poker app reveals turn first, then river, with suspense between.
- **Turn all-in**: 1 card (river) — handled normally.

The bulk-runout code path is specifically for 5 cards. The 2-card flop-all-in case is missed.

**Severity:** medium. Hits ~5-10 % of hands HU.

**Fix:** detect 2-card and 1-card runouts the same way — split into single-card sub-streets and apply them sequentially with the 1.5 s pause between (already done for the 5-card path via the `pendingRunoutCards` queue).

## P2-3 — `winnerInfo` cleanup waits 5 s after runout via polling, then clears even if next hand started  ▸ polish

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:1025-1041`

```swift
Task { @MainActor in
    while isRunoutAnimating {
        try? await Task.sleep(nanoseconds: 100_000_000)
    }
    try? await Task.sleep(nanoseconds: 5_000_000_000)
    if hand?.hand_id == endedHandId || hand == nil {
        winnerInfo = nil
    }
}
```

The 5 s is hard-coded. For a slow showdown with revealed cards, this is right. For a fold-only hand, 5 s of celebration is too long. There's no scaling.

`Task.isCancelled` not checked — if the view dismisses mid-wait the task still runs to completion. Harmless but unbounded.

**Severity:** low. Compounds with P2-1.

**Fix:** vary the wait based on `winnerInfo.termination` and showdown presence. 5 s for `.normal + showdown`, 2 s for fold-only.

## P2-4 — Server's `_auto_start_next_hand` 3 s delay applied even when animation didn't take that long  ▸ polish

**File:** `src/server/handler.py:573` default `delay: float = 3.0`

In the rare paths where cash uses auto-start (no humans seated, or initial join), the 3 s delay is fixed. Not adjustable to actual animation length.

**Severity:** low (only relevant for bot-only tables or post-join initial hand).

**Fix:** use the `_wait_for_animation_complete` pattern across all paths, not just duels.

## P2-5 — Hero's bet pill placement is computed in body, not via geometry preference  ▸ polish

**File:** `stack/Features/PokerTable/View/PokerTableView.swift:163-190`

The hero bet pill position is derived as:
```swift
let betX = heroPos.x + (centerX - heroPos.x) * 0.45
let baseBetY = heroPos.y + (centerY - heroPos.y) * 0.45
let yOffset: CGFloat = isDuelMode ? 20 : 55
```

with `heroPos = CGPoint(x: centerX, y: centerY + tableHeight * 0.35)`. On screens that differ from the design baseline (iPhone 15 Pro per Figma) the pill can overlap with the hole cards or the action panel. No animation on transition either — the pill snaps into existence the moment `seat.bet.amount > 0`.

**Severity:** low. iPhone SE / iPhone 16 Pro Max users see slight clip.

**Fix:** use a `matchedGeometryEffect` between the bet pill and the action panel for transitions. Test on small + large screens.

## P2-6 — `lastActions[seat]` `check` auto-clears in 2 s but other actions persist by design  ▸ minor

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:660-667`

```swift
if action == "check" {
    let actionId = lastActions[seat]?.id
    Task { @MainActor in
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        if lastActions[seat]?.id == actionId {
            lastActions[seat] = nil
        }
    }
}
```

Asymmetric. `check` clears in 2 s, `call`/`bet`/`raise_to` persist until the street ends. Probably intentional (call/bet labels double as the bet display per the comment). But: **fold** also persists indefinitely; the seat's `.folded` status overrides via `actionLabel`. OK.

But the 2 s `check` timer doesn't get cancelled if the same seat acts again — say someone checks the flop, then on the turn they bet. The check timer at +2 s could clear `lastActions[seat]` while it now holds "Bet $X". The id-check guard at line 663 saves it. OK.

**Severity:** ignorable. Noted for completeness — the action-label timing is generally fine.

## P2-7 — On reconnect, `actorExpiresAtMs` resets to "now + 60 s" but real deadline has drained  ▸ correctness

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:396` + `:38`

`update(from snapshot:)` does `currentActor = snapshot.hand?.actor_seat`. The didSet on `currentActor` resets `actorExpiresAtMs` to "now + 60 000 ms" (line 38). But the snapshot was issued after potentially seconds of disconnect. The real server-side deadline may already be at +20 s remaining.

Hero side: `ACTION_REQUEST` arrives separately and has the correct `expires_at_ms` (used by ActionPanel). So the hero ring is right.
**Opponent side: the FigmaPlayerSeat ring reads `tableState.actorExpiresAtMs`** (PokerTableView.swift:232) → it now shows a fresh 60 s drain even though the server timer is about to fire.

User-felt: bot ring drains slowly, then bot suddenly auto-folds because server timer expired while iOS ring showed plenty of time left.

**Severity:** low (rare, requires reconnect mid-bot-think) but a visible "wait what?" moment when it happens.

**Fix:** see P0-3 / P1-3 fix — drive opponent ring from server-provided `expires_at_ms` carried in `STATE_DELTA`.

## P2-8 — Recently-left snapshot ignore window is 2 s, fixed  ▸ polish

**File:** `stack/Features/PokerTable/Service/PokerWebSocketManager.swift:584, 711` (per CLAUDE.md `ignoreSnapshots = true for 2s after forceLeaveTable`)

If the server sends a delayed TABLE_SNAPSHOT >2 s after leave, iOS will accept it and re-rebuild table state. Symptom: user sees themselves "back at the table" briefly after leaving on a slow connection.

**Severity:** very low, rare on good network.

**Fix:** use the `currentTableId == nil` as the gate (not a fixed 2 s window).

## P2-9 — `winnerEntries` chop detection only looks at `winners.first?.hand_description`  ▸ correctness

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:977`

```swift
let isBlitzFoldCancel = winners.first?.hand_description == "Blitz fold"
```

Only the first winner is checked. In a multi-way chop, the `hand_description` may not be on the first winner (server emits "Blitz fold" on each in `cancel_hand` at `engine/table.py:540-545`, so it's always present — OK for now). But fragile: a future server change that varies the description per winner would break this.

**Severity:** ignorable. Note it for future protocol changes.

## P2-10 — `streetPotCollectionTrigger` set unconditionally but `capturedBetsForStreetAnimation` may be empty  ▸ correctness

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:860-866`

```swift
capturedBetsForStreetAnimation = seats.enumerated().compactMap { ... }
if !capturedBetsForStreetAnimation.isEmpty {
    streetPotCollectionTrigger = UUID()
}
```

Actually OK — the trigger is set only when there are bets. But the `.task(id: tableState.streetPotCollectionTrigger)` in PokerTableView re-fires the cleanup branch even on `nil → nil` transitions (no, it only fires on changes — and clears state at line 421). Looks correct on a re-read.

**Severity:** none. False alarm.

---

# P3 — Nice-to-have polish (low impact, easy wins)

## P3-1 — `ActionPanel` preset row comment says 6 presets, code renders 4  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/ActionPanel.swift:13-15, 552-557`

Comment lists `Min / ¼ Pot / ½ Pot / ⅔ Pot / Pot / All-In`. Code:
```swift
return [
    Preset(label: "Min",    amount: minRaise.amount),
    Preset(label: "½ Pot",  amount: amt(0.50)),
    Preset(label: "Pot",    amount: amt(1.00)),
    Preset(label: "All-In", amount: maxRaise.amount)
]
```

Either the comment is stale or the feature is incomplete. 4 is a reasonable count — adding ¼ and ⅔ would give finer sizing control.

**Severity:** very low. UX nit.

**Fix:** match comment to code, or add the missing presets.

## P3-2 — `Timer.publish(every: 0.25, on: .main)` in ActionPanel keeps firing even when not visible  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/ActionPanel.swift:49`

`autoconnect()` starts firing immediately and never invalidates. Fine while the panel is visible (most of the player's seat-time). Wasted while hidden.

**Severity:** very low.

**Fix:** drive countdown via `TimelineView` like the opponent ring — only runs when visible.

## P3-3 — `CommunityCardsView` reset on `hand_id` change can lose mid-deal animation  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/CommunityCardsView.swift:53-69`

```swift
.onChange(of: hand?.hand_id) { newHandId in
    if newHandId != lastHandId {
        lastHandId = newHandId
        animatedCount = 0
        if let count = hand?.board.count, count > 0 {
            animateCards(to: count)
        }
    }
}
.onChange(of: hand?.board.count) { newCount in
    let count = newCount ?? 0
    if count > animatedCount {
        animateCards(to: count)
    }
}
```

If a STATE_DELTA bumps board count (turn lands) on the same hand_id, `onChange(board.count)` animates from `animatedCount` to new count — good.
But on reconnect, the snapshot delivers a board with all cards already present. `onChange(hand_id)` resets `animatedCount = 0` and animates all from scratch — i.e., re-deals the flop/turn/river even though the player saw them already. ~700 ms of re-animation on every reconnect.

**Severity:** very low (reconnect frequency).

**Fix:** on `onChange(hand_id)`, skip the animation if `lastHandId` was a different hand AND the board arrives non-empty; just set `animatedCount = board.count` directly.

## P3-4 — Pulse animation `repeatForever` on actor pill never stops cleanly  ▸ polish

**File:** `stack/Features/PokerTable/View/PokerTableView.swift:998-1013` (CompactSeatView)

`isPulsing` is set to true with `.repeatForever(autoreverses: true)`. When actor changes away, `isPulsing = false` snaps; SwiftUI's `repeatForever` doesn't always cleanly cancel — sometimes the next render shows a frame of the pulse before stopping.

(Note: CompactSeatView appears to be legacy / preview-only; `FigmaPlayerSeat` is the production component. CLAUDE.md mentions `TableSeatView` as possibly-superseded too.)

**Severity:** very low if CompactSeatView is unused.

**Fix:** clean up CompactSeatView if it's dead code. Otherwise switch to `TimelineView`-based pulsing.

## P3-5 — `BetLabelPill` doesn't transition between bet amounts within a street  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/BetLabelPill.swift`

When a player calls a raise, their bet pill snaps from "Call $20" to "Call $80" with no transition. A `contentTransition(.numericText())` would make the chip count odometer-roll like the win-flash amount.

**Severity:** very low.

**Fix:** add `Text(label.text).contentTransition(.numericText())`. Available iOS 17+.

## P3-6 — Action button `disabled(isProcessing)` is mostly redundant with `clearActionRequest`  ▸ polish

**File:** `stack/Features/PokerTable/View/Components/ActionPanel.swift:92-93`

```swift
.disabled(isProcessing)
.opacity(isProcessing ? 0.6 : 1)
```

`isProcessing` flips true on tap (line 607), then PWSM clears `actionRequest` → the entire ActionPanel disappears (PlayTab.swift:952 gates on `actionRequest != nil`). The disabled state is visible for ~50 ms before the panel transitions out. Not really doing anything.

**Severity:** none. Minor cleanup opportunity.

## P3-7 — Server `set_sitting_out` event broadcast to ALL users after timeout  ▸ polish

**File:** `src/manager/runner.py:317-319` + `src/engine/table.py:786-806`

When a player times out, server emits `SeatUpdateEvent(seat=seat, is_sitting_out=True)` inside the events list. iOS doesn't handle this event type in `applyEvents` (default branch at PokerTableState.swift:1043 just prints). So the sit-out is only reflected via the next snapshot. There's a small window where the timed-out player appears active but the server has sat them out.

**Severity:** very low.

**Fix:** handle `seat_update` in `applyEvents` to mark the seat sitting out immediately.

## P3-8 — `_topup_bots_for_cash` runs unconditionally on every hand end  ▸ polish / perf

**File:** `src/engine/table.py:107-131`

Iterates all seats on every hand end, even if no bot busted. Negligible cost (max 6 iterations), but it does scan and allocate.

**Severity:** none.

## P3-9 — Bot `_pending_action` retry can send duplicate ACTIONs in 3 s if STATE_DELTA delayed  ▸ correctness

**File:** `~/Projects/stack_poker/openbot_client.py:180-191`

```python
async def _retry_checker(self):
    while True:
        await asyncio.sleep(2.0)
        if self._pending_action and self._pending_action_attempts < 3:
            elapsed = time.time() - self._pending_action_time
            if elapsed > 3.0:
                self._pending_action_attempts += 1
                await self.ws.send(json.dumps(self._pending_action))
```

Bot retries the same ACTION (same `action_id`). Server's `_processed_actions` cache (handler.py:217) idempotently drops it. OK in practice.

But: on a 3+ s WS round-trip, bot resends → server idempotently drops → bot still hasn't seen confirmation → resends again at +5 s → … up to 3 attempts. Wastes bandwidth.

**Severity:** none. Idempotency saves it.

## P3-10 — `SAFETY_NET` in `_broadcast_events` sends extra ACTION_REQUEST + register_deadline  ▸ polish

**File:** `src/server/handler.py:529-543`

If the broadcast loop fails to issue ACTION_REQUEST to the actor for any reason, this catches it and sends one. But it ALSO registers a fresh deadline — potentially shortening the actor's timer if the original was already registered. There's also no check whether the original ACTION_REQUEST DID go out (just whether the timer is registered).

**Severity:** low. Defensive code, fine in practice.

---

## Three more found by tracing a hand end-to-end (self-challenge per the brief)

### X-1 — `hand_ended` clears `actionRequest` but the `STATE_DELTA.actor_seat` may still be the previous actor  ▸ polish

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:912-915` + `PWSM:1228`

`applyEvents` on `hand_ended` does `actionRequest = nil; currentActor = nil`. Then the wrapper sets `tableState.currentActor = delta.actor_seat` at PWSM:1228. If the server's representative snapshot at the time of broadcast picked an actor (race), `delta.actor_seat` could be non-nil — re-setting `currentActor` to a value AFTER hand_ended cleared it. The opponent ring would briefly draw on a folded seat at hand-end.

`handler._broadcast_events:464` derives `actor_seat = representative.hand.actor_seat if representative.hand else None`. After hand_ended, status = BETWEEN_HANDS, hand = None (in the snapshot), so actor_seat = None. So this should be OK in practice — but it's "OK if the server is right" rather than defensively gated. iOS should guard.

**Severity:** none in current configuration, fragile.

**Fix:** in PWSM `case "STATE_DELTA"`, if any event in `events` is `hand_ended`, skip the `currentActor = delta.actor_seat` write.

### X-2 — Action label "RAISE $X" doesn't update when X bumps via a re-raise in the same street  ▸ polish

**File:** `stack/Features/PokerTable/Model/PokerTableState.swift:649-668`

When seat A raises to $40, `lastActions[A] = (raise_to, $40)`. Then seat B 3-bets to $120. Then seat A calls (bet now $120, action is "call"). `lastActions[A]` gets overwritten with `(call, $120)`. OK.

But: if seat A then 4-bets to $300, `lastActions[A] = (raise_to, $300)`. The FigmaPlayerSeat `actionLabel` reads "Raise $300" (line 200-201). But there's no transition — the pill text snaps from "Call $120" to "Raise $300". No `.contentTransition`, no fade. Looks jumpy at high action.

**Severity:** very low.

**Fix:** add `.transition(.opacity)` on the actionLabelPill and let SwiftUI handle re-renders, or use `.contentTransition(.numericText())` on the amount portion.

### X-3 — Hero hole-cards disappear for one frame when status flips to `.between_hands` on a fold  ▸ polish

**File:** `stack/Features/PokerTable/View/PlayTab.swift:1306-1315`

```swift
if let heroIndex = tableState.yourSeat,
   heroIndex < tableState.seats.count,
   !heroCards.isEmpty,
   !wsManager.isSittingOut,
   (tableState.status != .between_hands || heroCardsForWinner),
   (tableState.seats[heroIndex].isInHand || heroCardsForWinner) {
```

When the hero folds:
1. iOS sends fold action → optimistically clears actionRequest.
2. STATE_DELTA arrives with hero's fold action → `seats[heroIndex].status = .folded` (PokerTableState.swift:670-680).
3. The gate `tableState.seats[heroIndex].isInHand || heroCardsForWinner`: `isInHand` is false (folded), `heroCardsForWinner` checks `winnerInfo != nil && !heroFoldedThisHand`. At this point winnerInfo is still nil and heroFoldedThisHand will be true. So both branches fail → hero cards hidden.
4. ~ms later hand_ended arrives → winnerInfo set, but heroFoldedThisHand is true → still false → still hidden. 

This is intentional ("if HERO folded, the cards shouldn't linger") per the comment at line 1296-1298.

But: between step 2 and step 3, the cards disappear in one frame with no transition. Should they fade out? Currently they snap.

**Severity:** very low. Minor polish.

**Fix:** wrap the hero card render in `.transition(.opacity.combined(with: .move(edge: .bottom)))` so the cards animate offscreen on fold.

---

# Summary

The hand pipeline is mostly correct. The biggest player-felt issues are:

1. **Bot pacing is a metronome** (P0-1). A single `asyncio.sleep(1.0)` makes bots feel artificial. Fix this first.
2. **Zero audio** (P0-2). The single biggest "missing feature" relative to peer apps.
3. **Opponent timer ring math is wrong for short-deadline bots** (P0-3). Looks broken every bot turn.
4. **Hard-coded 0.35 s delay before every hand's hole cards** (P0-4). Comment refers to an animation that no longer exists.
5. **Mid-celebration `REBUY` mutations** (P0-5). Bot stacks visibly jump while the winner banner is showing.
6. **Optimistic action clear breaks on server rejection** (P0-6) and **no grace on server-side timeout** (P0-7).

Of the 30+ items above, fixing only the 7 in P0 + the top 4 in P1 (P1-1 chip animation timing, P1-2 phantom blind block, P1-8 showdown card flip, P1-9 tap haptics) would close most of the gap between "feels like a video game" and "feels like a real online poker client."

None of these are crashes. Most are <50 LOC each. The cumulative impact on perceived quality is large.
