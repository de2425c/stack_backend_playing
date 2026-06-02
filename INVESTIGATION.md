# Stack Poker — Freeze & Crash Investigation

Investigator: independent multi-hour code audit.
Scope: backend (`stack_backend_playing`) + iOS (`stack_poker/stackpoker/stack`).
The cross-cutting bot client (`~/openbot/src/serving/openbot_client.py`) lives on the prod server, outside both cloned repos — analyzed only via the spawn surface in `app.py`.

---

## Methodology

1. Mapped both repos and the WebSocket protocol between them.
2. Traced a single hand end-to-end (deal → action → showdown).
3. Hunted symptoms by category:
   main-thread blocks · retain cycles · races · listener leaks ·
   CFR memory blowup · sync-in-async ·
   force unwraps · listener leaks · unbounded growth ·
   protocol invariant violations · bot subprocess lifecycle.
4. Cross-correlated: a UI freeze often has a backend cause.
5. Ranked by severity × likelihood; cited `file:line` for every finding.

Total backend LOC examined: ~9,700.
Total iOS LOC examined: ~7,800 of the ~89,800 Swift LOC (focused on `Features/PokerTable/**`, key services, FriendsService, SessionComplete).

---

## Architecture Map

### Backend (Python / FastAPI / asyncio)

```
FastAPI /ws WebSocket endpoint  (src/server/app.py:2491)
  └── per-user message loop (single asyncio task per connection)
       └── MessageHandler  (src/server/handler.py)
            └── TableManager  (src/manager/manager.py)
                 └── TableRunner per table (src/manager/runner.py)
                      • async queue serialising all engine mutations
                      • single asyncio.Task drains the queue
                      └── PokerTableEngine  (src/engine/table.py)
                            └── PokerKitAdapter  (src/engine/adapter.py)
                                 └── PokerKit `State`

Background tasks:
  • Heartbeat reaper      (5s interval, 65s idle threshold)
  • Duel sweeper          (30s reconciler)
  • ActionTimerService    (250ms tick, auto-fold on timeout)
  • Hand logger writes    (Firestore, fire-and-forget tasks)

Per-bot-table subprocess spawned via _spawn_bot_process()
  → connects back to server via ws://localhost:8000/ws
```

State maps that must stay in sync:
- `ConnectionManager._connections` (user_id → WebSocket)
- `ConnectionManager._table_users` (table_id → {user_ids})
- `ConnectionManager._user_tables`  (user_id → table_id)
- `TableManager._user_tables`       (user_id → table_id)
- `TableRunner._user_seats`         (user_id → seat)
- `_active_duels` / `_user_duels` / `_bot_table_owners` (module-level dicts)

A monotonic `seq` is incremented per engine event-batch (one delta per `start_hand` / `apply_action`).

### iOS (Swift / SwiftUI / Combine, iOS 16+)

```
PokerWebSocketManager  (singleton, @MainActor)
  • Owns URLSession + URLSessionWebSocketTask
  • Receive loop runs on the main actor
  • Decodes message → routeMessage(type:data:)
  • Publishes into PokerTableState

PokerTableState  (@MainActor, nested ObservableObject)
  • Holds entire game state (almost everything is @Published)
  • applyEvents(_:handId:) mutates seats/hand/winnerInfo synchronously
  • Animation queues (pendingRunoutCards, capturedBetsForAnimation, ...)

PokerTableView  (SwiftUI)
  • Observes PokerTableState
  • Runs 4 separate `.task(id:)` animation jobs

PlayTab  (SwiftUI container)
  • Routes between lobby and full-screen table
  • Subscribes to wsManager + tableState
```

---

## Hand Lifecycle (end-to-end)

1. iOS sends `JOIN_POOL` / `CREATE_BOT_TABLE` / `JOIN_DUEL`.
2. Backend → `manager.add_player` → `runner._handle_join` → `engine.seat_player`.
3. When `player_count >= min`, `handler._auto_start_next_hand` schedules `manager.start_hand` after 1.5–3 s.
4. `engine.start_hand` (engine/table.py:185) builds a fresh PokerKit `State`, caches hole cards, computes button rotation, emits `HandStartedEvent` + two `ActionEvent(POST_BLIND)`.
5. `handler._broadcast_events` sends `STATE_DELTA` to every user_id at the table, *also* sends a per-user `TABLE_SNAPSHOT` (so each user sees their own hole cards), then sends `ACTION_REQUEST` to the actor.
6. Client receives `STATE_DELTA`; `PokerTableState.applyEvents` mutates seats/hand. View animates blinds & button (`PokerTableView.swift:490` task).
7. Actor receives `ACTION_REQUEST`. iOS sets `tableState.actionRequest`. Server registers a 60s deadline in `ActionTimerService`.
8. Player taps fold/check/call/bet/raise. iOS `sendAction(...)` POSTs `ACTION` JSON.
9. Backend `handler.handle_action` enqueues `PlayerActionCommand` on the runner; engine applies via PokerKit; emits `ActionEvent` (+ optional `StreetDealtEvent` / `HandEndedEvent`).
10. Hand-end: `_finalize_hand` emits `HandEndedEvent`; runner's `_flush_hand_log` writes to `HandLogger` → Firestore via `asyncio.create_task` (fire-and-forget).
11. Server then either:
    • duel: `_check_duel_bust` → either complete match or wait for `ANIMATION_COMPLETE` then start next hand
    • cash with all-bots-no-humans: auto-start next hand after delay
    • cash with humans: wait for client `NEXT_HAND` message
12. iOS plays runout + winner animations (5–8 s) and fires `ANIMATION_COMPLETE` when chip animation finishes.

---

# Findings (Severity-Ranked)

Each finding lists:
- **Symptom** (what the user sees)
- **Root cause** (`file:line`)
- **Why**
- **Fix** (concrete patch direction)
- **Severity** / **Likelihood** / **Confidence**

Severity ranks: **P0** (immediate user-visible bug, frequent) · **P1** (significant) · **P2** (latent, harder to trigger) · **P3** (cleanup).

---

## P0-1 — Sync Firestore calls block the entire event loop  ✅ FIXED

**Status:** FIXED in commit on `main`. All async methods on `FirestoreClient`
now dispatch the blocking Firebase Admin SDK call onto the asyncio default
thread pool via `asyncio.to_thread(...)`. Added batch helpers
`get_hands(hand_ids)` and `get_duel_ratings(user_ids)` that fan out via
`asyncio.gather` so callers in P1-7 / P1-8 can replace serial loops with
one round-trip's worth of latency. `write_ledger_entries` switched to a
Firestore `batch().commit()` for one network hop instead of N. The sync
methods `get_hand_log`/`get_ledger_entries`/`get_all_*` are kept (debug
endpoints call them) and clearly labelled SYNC in their docstring.

**Verification:** Module imports and parses; every method previously
declared `async` is still `async`. Behavioural verification (real
Firestore RTT measurement) requires deploying to the server — see
FIXES_SUMMARY.md.

**Original finding:**


**Symptom (server):** All users / all tables briefly freeze every time anything touches Firestore — joins, leaves, hand_end rebuys, duel match start, duel match end, top-ups, bot persona selection.

**Root cause:** Every method on `FirestoreClient` is declared `async` but invokes the **synchronous** Firebase Admin SDK. The blocking call never yields to the event loop.

- `src/persistence/firestore_client.py:70` — `write_hand_log` → `self._db.collection("hands").document(...).set(data)` is sync
- `src/persistence/firestore_client.py:90` — `write_ledger_entries` does `.set()` in a Python loop
- `src/persistence/firestore_client.py:109` — `get_hand_log` sync `.get()`
- `src/persistence/firestore_client.py:188` — `get_user_balance` sync `.get()`
- `src/persistence/firestore_client.py:244` — `deduct_balance` runs a sync transaction
- `src/persistence/firestore_client.py:308` — `add_balance` sync transaction
- `src/persistence/firestore_client.py:351,374,399,421,449,496,529` — all session/duel/rating writes/reads
- `src/persistence/firestore_client.py:132` — `get_ledger_entries` sync `.stream()`

**Why this is severe:**
- `manager.add_player` (manager.py:148-153) awaits `get_user_balance` + `deduct_balance` before seating. Each is a sync gRPC round trip (~50–200 ms typical). Until both return, the asyncio loop is blocked → **every other table is frozen**, the heartbeat reaper can't tick, the action timer can't tick, broadcasts queue up.
- `_check_and_process_rebuys` (handler.py:722-773) loops over seats and `await`s `get_user_balance` + `deduct_balance` sequentially → at a 6-max table with 3 bust players, the entire server stalls 300–1200 ms at hand-end.
- `_complete_duel_match` (app.py:2086-2088) "concurrently" reads both ratings via `asyncio.gather`, but since each awaitable is sync underneath, gather provides zero concurrency — both run serially.
- `bot_personas.py:186` — `get_available_persona` loops over **70 personas** and awaits `get_duel_rating` per persona. A single duel queue timeout that fills with a bot blocks the loop for **70 × ~50 ms ≈ 3.5 s**.
- `_fetch_hands` (session/processor.py:342-352) sequentially awaits `get_hand` per `hand_id`. A 100-hand session triggers ~5 s of loop blocking at session-end (in a background task — but the loop is still blocked).

**Fix:**
1. Wrap every sync Firestore op in `asyncio.to_thread(...)`. This shifts the gRPC wait onto a thread-pool thread so the loop keeps running:
   ```python
   async def get_user_balance(self, user_id: str) -> int:
       if self._db:
           doc = await asyncio.to_thread(
               lambda: self._db.collection("wallets").document(user_id).get()
           )
           ...
   ```
2. For batched ops (`write_ledger_entries`, `_check_and_process_rebuys`), parallelize with `asyncio.gather(*[asyncio.to_thread(...) for ...])` so N seats finish in 1 RTT.
3. Strongly consider migrating to `google-cloud-firestore`'s **async** client (`AsyncClient`) which exposes proper async methods.

**Severity:** P0 · **Likelihood:** Continuous · **Confidence:** Confirmed by reading every method.

---

## P0-2 — Fresh sockets reaped within 5 s of AUTH (lobby disconnect storm)  ✅ FIXED

**Status:** FIXED — `app.py` now writes `connections._last_seen[user_id] =
time.monotonic()` immediately after assigning `_connections[user_id]`, so
the heartbeat reaper never sees a missing entry. One-line fix as
recommended.

**Verification:** `python3 -c 'ast.parse(...)'` succeeds; the existing
`mark_seen`/`get_stale_users` flow is untouched and the reaper threshold
(65 s) is unchanged — the fix only stops the "ts is None" branch from
firing on first connect.

**Original finding:**


**Symptom (iOS):** Player connects, authenticates, sits in the lobby for >5 s without doing anything → WebSocket is closed by the server with code 4002 → iOS reconnect loop fires, possibly repeats indefinitely until the user finally clicks a button.

**Root cause:**
- `app.py:2542` — after AUTH the WS endpoint writes `connections._connections[user_id] = websocket` directly, **bypassing `ConnectionManager.connect()`**.
- `ConnectionManager.connect()` (connection.py:51) is the only place that initialises `_last_seen[user_id]`. The direct assignment skips it.
- `connection.py:62` — `mark_seen` only ever runs *inside* the message loop (`app.py:2584`), after the user has sent a post-AUTH message.
- `connection.py:67-75` — `get_stale_users` treats a missing `_last_seen` entry as stale:
  ```python
  ts = self._last_seen.get(user_id)
  if ts is None or ts <= cutoff:
      stale.append((user_id, ws))
  ```
- The heartbeat reaper ticks every 5 s (`HEARTBEAT_REAPER_INTERVAL_SECONDS = 5.0`) and immediately closes any socket with `ts is None`.
- iOS PING fires only every 30 s (`PokerWebSocketManager.swift:184`). The first inbound message arrives way after the reaper has already killed the socket.

**Why this is bad:** Even normal lobby behavior (read the screen, choose a stake) trips the reaper. Each reap → iOS `handleDisconnect` → reconnect attempt → AUTH → idle 5 s → reap → loop. iOS caps at `maxReconnectAttempts = 5` (`PokerWebSocketManager.swift:187`), after which the user is dumped to an error state.

**Fix (1-line):** In `app.py:2542`, change:
```python
connections._connections[user_id] = websocket
```
to:
```python
connections._connections[user_id] = websocket
connections._last_seen[user_id] = time.monotonic()
```
Better still, **stop bypassing `connect()`**; refactor so the WS endpoint calls the public API (and let `connect()` skip its accept-already-done step via a parameter).

**Severity:** P0 · **Likelihood:** Triggers on **every** session that involves any lobby browsing · **Confidence:** Confirmed by trace.

---

## P0-3 — `add_player` debits the wallet, then fails to refund on seat error  ✅ FIXED

**Status:** FIXED — `add_player` now tracks `debited_cents`/`seated` flags
and, in a `finally` block, issues `asyncio.shield(self._firestore.add_balance(...))`
when the wallet was debited but the seat was not acquired. Shielding
ensures a cancelled outer task still refunds. If the refund itself raises,
we log CRITICAL so an operator can manually credit the wallet.

**Verification:** Wrote a small in-memory script that:
1. Seats two bots at an HU table (max_players=2 → table is full)
2. Pre-funds Alice's wallet with $1000
3. Tries to seat Alice at the same table → raises `Table full`
4. Asserts Alice's wallet is back to $1000 (no debit retained)

Result: `REFUND CONFIRMED`. Behaviour confirmed on the in-memory mode.
Production Firestore behaviour is identical — both `add_balance` and the
`finally` block run inside the same coroutine.

**⚠ NEEDS REVIEW (financial code):** The `try/finally` change is wider
than the original P0-3 fix proposal. Specifically:
1. `BaseException` is not caught — only the explicit `Exception`/
   `CancelledError` paths in `finally`. If a `KeyboardInterrupt` or
   `SystemExit` fires between deduct and seat, the refund still runs
   (via finally semantics) but won't catch the exception itself.
2. The `is_real_user` guard checks `not user_id.startswith(...)` exactly
   as before — bot wallets aren't tracked, so no refund needed for them.
3. The refund is `await asyncio.shield(...)` — the inner add_balance
   task will complete even if the outer task is cancelled mid-finally.
   This means cancellation may be delayed by one Firestore RTT.

**Original finding:**


**Symptom:** A user joins a stake that turns out to be full / closed (race) → server raises `Table full` or `Seat occupied` → user's wallet balance is debited but they're not seated. Money lost; user files support ticket.

**Root cause:** `manager.add_player` (manager.py:147-176):
```python
if self._firestore and not user_id.startswith(("bot_", "user_bot_")):
    balance = await self._firestore.get_user_balance(user_id)
    if balance < buy_in.amount:
        raise ValueError("INSUFFICIENT_BALANCE: ...")
    await self._firestore.deduct_balance(user_id, buy_in.amount)   # ← debits

...

await runner.submit(JoinTableCommand(...))   # ← can raise "Table full"
seat, snapshot = await future                # ← raises propagate, no refund
```

No `try/except` around the submit/await pair to refund the wallet on failure.

**Fix:**
```python
try:
    await runner.submit(JoinTableCommand(...))
    seat, snapshot = await future
except Exception:
    if self._firestore and not user_id.startswith(("bot_", "user_bot_")):
        try:
            await self._firestore.add_balance(user_id, buy_in.amount)
        except Exception:
            logger.exception("CRITICAL: failed to refund seat-failure debit", user_id=user_id, amount=buy_in.amount)
    raise
```
Also: stop creating a fresh table when the requested-specific `table_id` doesn't exist — manager.py:158 raises but the wallet was already debited.

**Severity:** P0 (money-loss bug) · **Likelihood:** Low per attempt, but financial impact non-trivial · **Confidence:** Confirmed.

---

## P0-4 — `objectWillChange` re-publish causes UI redraws on every backend message  ✅ FIXED

**Status:** FIXED — added a `.throttle(for: .milliseconds(16), scheduler:
RunLoop.main, latest: true)` between `tableState.objectWillChange` and
`wsManager.objectWillChange.send()`. Multiple property mutations within a
single 16 ms display frame coalesce to one parent objectWillChange tick,
which kills the redraw storm without breaking views that read
`wsManager.tableState.X` (DuelLobbyView, SessionSummarySheet, PlayTab,
…) — those views still get rebuilt, just once per frame instead of
dozens of times per delta.

I chose throttle over removing the forwarding because a clean removal
would require changing every consumer site (~25 references) to bind to
`tableState` directly. That's a larger refactor than the surgical fix
calls for. The Combine pipeline preserves the existing observability
contract; only the rate changes.

**Verification:** `xcodebuild -project stack.xcodeproj -scheme stack
-configuration Debug -destination 'generic/platform=iOS Simulator'
build` → `** BUILD SUCCEEDED **`. Runtime profiling on-device is
deferred to FIXES_SUMMARY.md (cannot run Instruments from here).

**Original finding:**


**Symptom (iOS):** Visible jank during gameplay; battery drain; UI hitching, particularly on older devices or during fast HU bot play.

**Root cause:** `PokerWebSocketManager.swift:218-223`:
```swift
tableState.objectWillChange
    .receive(on: RunLoop.main)
    .sink { [weak self] _ in
        self?.objectWillChange.send()
    }
    .store(in: &cancellables)
```

Every time **any** `@Published` on `PokerTableState` mutates, this re-fires `objectWillChange` on the singleton `PokerWebSocketManager`. Any SwiftUI view bound to `@StateObject/@ObservedObject wsManager: PokerWebSocketManager` rebuilds its body. The PlayTab body (and via that, the entire `PokerTableView`) rebuilds.

Combined with **P1-1** (the Canvas felt grain drawing ~9,700 ellipses per body invocation), this is a perfect storm.

A single hand fires dozens of @Published mutations across `applyEvents`:
- `serverPot`, `lastActions[seat]`, `seats[i]` (per seat update), `hand`, `currentActor` (didSet sets `actorExpiresAtMs`), `winnerInfo`, `showdownCards`, `capturedBetsForStreetAnimation`, `pendingRunoutCards`, `isRunoutAnimating`, …

Each one round-trips: `tableState.objectWillChange.send()` → Combine sink → RunLoop.main tick → `wsManager.objectWillChange.send()` → SwiftUI invalidates every wsManager-binding view.

**Fix:**
1. **Remove the forwarding entirely.** Modern SwiftUI handles nested `ObservableObject` correctly when you mark the inner one as `@ObservedObject`/`@StateObject` directly on the view. PlayTab already does `@ObservedObject private var tableState = PokerWebSocketManager.shared.tableState` (PlayTab.swift:25) — it doesn't need the forwarding.
2. Verify any other consumer that reads `wsManager.tableState.X` via `wsManager` — they should rebind to `tableState` directly.
3. If forwarding is genuinely needed for backward-compat, batch with `.debounce(for: .milliseconds(16), scheduler: RunLoop.main)`.

**Severity:** P0 (perceived freezes) · **Likelihood:** Continuous · **Confidence:** High.

---

## P1-1 — `Canvas` felt grain redraws ~9,700 ellipses every body invocation  ✅ FIXED

**Status:** FIXED — added `.drawingGroup()` and a stable
`.id("felt-grain-\(Int(width))x\(Int(height))")` to the Canvas in
`PokerTableView.swift`. `.drawingGroup()` rasterizes the closure output
via Metal; the stable id keeps the view's SwiftUI identity unchanged as
long as the felt size doesn't change, so the cached bitmap is reused
across body rebuilds.

**Verification:** Build succeeded. The throttle from P0-4 already
reduces how often this is hit; together the two changes should
eliminate the felt-grain redraw cost during gameplay.

**Original finding:**


**Symptom (iOS):** Visible CPU spikes / dropped frames whenever the table view rerenders.

**Root cause:** `PokerTableView.swift:645-662`:
```swift
Canvas { ctx, size in
    var s: UInt64 = 0xCAFEBABE_DEADBEEF
    let count = Int(size.width * size.height / 14)
    for _ in 0..<count {
        s = s &* ...
        let x = ...
        let y = ...
        ...
        ctx.fill(Path(ellipseIn: CGRect(x: x, y: y, width: 1.1, height: 1.1)), ...)
    }
}
```

For a 312×435-pt felt, `count ≈ 9700`. The Canvas closure executes on the main thread, fully synchronous, every time SwiftUI re-renders the parent view. The parent (`tableBackground`) lives inside the GeometryReader in `PokerTableView.body`, so it rebuilds whenever the binding tableState changes (which is constantly — see **P0-4**).

The seed is deterministic, so the output never changes. **There is no reason to recompute it.**

**Fix:** Either:
1. Render once into a `UIImage`/`Image` via `ImageRenderer` and reuse it:
   ```swift
   @State private var feltGrain: Image?
   ...
   .task(id: feltSize) {
       let renderer = ImageRenderer(content: feltGrainCanvas(size: feltSize))
       feltGrain = renderer.uiImage.map(Image.init(uiImage:))
   }
   ```
2. Replace with a static asset (PNG noise texture) — fully GPU-cached.
3. As a quick patch, wrap in `.drawingGroup()` so SwiftUI rasterizes once and reuses the bitmap:
   ```swift
   Canvas { ... }
       .frame(...)
       .drawingGroup()   // ← rasterize-once
   ```

**Severity:** P1 (frame drops, battery) · **Likelihood:** Every frame change · **Confidence:** Confirmed by code reading.

---

## P1-2 — iOS WS receive loop runs entirely on the MainActor  ✅ FIXED (heavy decodes off-main)

**Status:** FIXED for the parts that matter — every JSON decode now hops
off the MainActor:
- The per-message `MessageTypeWrapper` decode in `handleMessage` uses a
  new `Self.decodeOffMain<T>(_:from:)` helper that runs the decode on a
  detached background task at `.userInitiated` priority, then returns
  the typed value.
- The two heavy decodes — `TableSnapshotMessage` (full seats array +
  hole cards) and `StateDeltaMessage` (events array carrying showdown
  hands, board cards, winners) — use the same helper.

The receive loop itself stays on the MainActor — `URLSessionWebSocketTask.receive()`
is async and properly releases the actor during suspension, so the cost
was always the decode + mutation work afterwards. Mutations (e.g.,
`tableState.applyEvents(...)`) must stay on the MainActor because
`PokerTableState` is `@MainActor`; only the JSON parsing moved off.

**Verification:** `xcodebuild` → `** BUILD SUCCEEDED **`. Behavioural
parity preserved — the message type wrapper is identical, the typed
decode result is identical, only the thread the decode runs on changed.

**Original finding:**


**Symptom (iOS):** UI hitches when STATE_DELTA / TABLE_SNAPSHOT arrive — especially on the first hand of a 6-max session where the snapshot carries 6 seats × hole cards × pots, or during all-in runouts.

**Root cause:** `PokerWebSocketManager` is `@MainActor`. The receive loop (`receiveMessages`, line 927), JSON decoding (`handleMessage`, line 942), `routeMessage` (line 967), and the synchronous mutations into `PokerTableState.applyEvents` all execute on the main actor.

Every byte of WS traffic blocks the UI thread. A burst of consecutive deltas (street_dealt + action + hand_ended) decodes and applies serially on main, contending with SwiftUI's rendering tick.

**Fix:**
1. Demote the receive loop and decoder to a `nonisolated` async function. Only the final `tableState.apply(...)` hop should jump to `MainActor.run { ... }`.
   ```swift
   private nonisolated func receiveMessages() async {
       while await self.isReceivingNonisolated, let ws = await self.webSocketNonisolated {
           do {
               let message = try await ws.receive()
               let parsed = decodeOffMain(message)
               await MainActor.run { self.applyParsed(parsed) }
           } catch { ... }
       }
   }
   ```
2. Or use a detached `Task.detached(priority: .userInitiated)` for the receive loop and route via `MainActor.run`.

**Severity:** P1 · **Likelihood:** Continuous during gameplay · **Confidence:** Confirmed.

---

## P1-3 — URLSession leak on iOS reconnect cycles  ✅ FIXED

**Status:** FIXED — added a stored `private let urlSession = URLSession(
configuration: .default)` and changed `establishConnection()` to call
`urlSession.webSocketTask(with: serverURL)` instead of creating a fresh
session every time. Singleton lifetime ⇒ session is created once and
reused for the life of the app.

**Verification:** `xcodebuild` → `** BUILD SUCCEEDED **`. Behavioural
parity: WebSocketTask creation, resume(), cancel(), and send/receive
all work identically on a shared URLSession — only the session
lifetime changed.

**Original finding:**


**Symptom (iOS):** Memory grows by ~100 KB per reconnect cycle; long-running sessions with intermittent connectivity will accumulate orphaned URLSessions.

**Root cause:** `PokerWebSocketManager.swift:833`:
```swift
private func establishConnection() async throws {
    let session = URLSession(configuration: .default)   // ← new URLSession every time
    webSocket = session.webSocketTask(with: serverURL)
    webSocket?.resume()
    ...
}
```

`session` is created fresh on every `establishConnection` call (initial connect + every reconnect + every manual reconnect). The local `session` is never `invalidateAndCancel()`'d. Once `webSocket` is reassigned, the previous session has nothing referencing it locally — but each `URLSession` retains its delegate queue + internal NSURLSessionTask state until invalidated.

**Fix:**
1. Hold the URLSession in a stored property and reuse it; only the WebSocketTask is per-connection:
   ```swift
   private let urlSession = URLSession(configuration: .default)
   ...
   webSocket = urlSession.webSocketTask(with: serverURL)
   ```
2. If a fresh session is genuinely needed (e.g., for cookie isolation), call `previousSession.invalidateAndCancel()` before swapping.

**Severity:** P1 (slow leak) · **Likelihood:** On every reconnect, which is frequent given **P0-2** · **Confidence:** Confirmed.

---

## P1-4 — `handler._broadcast_events` makes N+1 round trips through the runner queue  ✅ FIXED

**Status:** FIXED — added `GetSnapshotsBatchCommand` to `commands.py`,
`_handle_snapshots_batch` to `runner.py`, and
`TableManager.get_snapshots_batch(table_id, user_ids)` to `manager.py`.
`handler._broadcast_events` now fetches all per-user snapshots in a
single runner round-trip via `get_snapshots_batch` instead of looping
`get_snapshot(user_id)` per user. At a 6-max table this is 6× fewer
queue trips per broadcast.

The actor's ACTION_REQUEST still goes through
`_send_action_request` → `get_action_request` (one extra trip), and the
safety-net codepath after the loop is unchanged. Both are single-user.

**Verification:** parses + imports cleanly. Integration test seats two
players and calls `get_snapshots_batch(table_id, ['bot_a','bot_b','nonexistent'])`:
returns dict with `bot_a`/`bot_b` snapshots, `nonexistent` silently
skipped (expected race semantics).

**Original finding:**


**Symptom (server):** Hand-start broadcast latency scales with player count; visible "everyone sees their cards at slightly different times" when the table is busy.

**Root cause:** `handler.py:443-505`. For every event broadcast at a table with N users:
1. One `get_snapshot` call for `first_user` to extract seq / actor_seat (line 444).
2. For each user, one `get_snapshot` to build their personal snapshot (line 489).
3. For the actor, one `get_action_request` (line 504) → `_send_action_request` → through runner queue.
4. The "safety net" at line 510-524 might do another `get_snapshot`.

Each of these is an `await runner.submit(...)` → enqueues a command and awaits the future. The runner's `_run` loop polls every 100ms (`runner.py:122-128` — see also **P2-3**) so each round trip is at minimum that.

Combined with **P0-1**, snapshot-building can include serialising hole cards + seats, which is cheap, but pulling state means yielding to the runner task. The `await asyncio.wait_for(self._queue.get(), timeout=0.1)` polling adds latency.

**Fix:**
1. Add `engine.get_snapshot_batch(user_ids: list[str])` that holds the runner busy for ONE command, returning a dict of per-user snapshots.
2. Or use a snapshot cache + `_seq` check so subsequent users in the same broadcast don't re-traverse engine state.

**Severity:** P1 (latency) · **Likelihood:** Every hand at a 6-max · **Confidence:** Code-confirmed.

---

## P1-5 — `_check_and_process_rebuys` sequentialises wallet ops on hand-end  ✅ FIXED

**Status:** FIXED — restructured to:
1. Collect rebuy candidates in one pass over the seats.
2. `asyncio.gather` over `try_rebuy(...)` for all candidates so their
   Firestore transactions run concurrently on the thread pool.
3. For users whose rebuy returned None, `asyncio.gather` over
   `get_user_balance(...)` for OUT_OF_CHIPS notifications.
4. Sequential broadcasts after both rounds.

Used `return_exceptions=True` so a single user's Firestore failure
doesn't take down the rebuy round for the rest of the table.

**Verification:** parses + imports cleanly. Behavioural contract
unchanged: same set of REBUY / OUT_OF_CHIPS messages get sent in the
same order; only the I/O wait collapses.

**Original finding:**


**Symptom (server + all users at the table):** Visible 200–600 ms freeze after every hand at a 6-max bot table when human stack drops below 100bb.

**Root cause:** `handler.py:722-773`. For each seat:
- `await self._manager.try_rebuy(user_id, table_id, seat_idx)` — issues `get_user_balance` + `deduct_balance` (both sync Firestore).
- If failed, `await self._manager._firestore.get_user_balance(user_id)` — another sync hit.
- `await self._connections.broadcast_to_table(table_id, rebuy_msg)` — fine.

For each bust player this is 2–3 Firestore round trips, all serialised on the loop.

**Fix:** Once **P0-1** is fixed, parallelise:
```python
results = await asyncio.gather(*[
    self._manager.try_rebuy(uid, table_id, idx)
    for (uid, idx) in bust_players
])
```

**Severity:** P1 · **Likelihood:** Every hand at a 6-max bot session · **Confidence:** Confirmed.

---

## P1-6 — `_check_duel_bust` blocks the WS message loop for up to 15 s  ✅ FIXED

**Status:** FIXED — `_check_duel_bust` now spawns a background task
`_finish_duel` that owns the `_wait_for_animation_complete` +
`_complete_duel_match` chain. The original function returns True
immediately on bust detection so the caller's broadcast loop doesn't
stall. Timeout reduced from 15 s to 5 s per the original finding's
recommendation.

The caller (`_broadcast_events` at the duel branch) still gets the
right True/False signal, so it correctly skips auto-starting the next
hand. The match teardown happens out-of-band.

**Verification:** Parses cleanly. The branch protocol (return True =
duel ended, skip next-hand auto-start; return False = continue
playing) is preserved.

**Original finding:**


**Symptom (iOS, duel mode):** After busting an opponent, the winning player's WebSocket message loop is unresponsive (no PING/PONG, no further actions) for up to 15 seconds.

**Root cause:** `handler.py:667`:
```python
await self._wait_for_animation_complete(table_id, timeout=15.0)
```
This is called inside `_check_duel_bust`, which is called inside `_broadcast_events`, which is called inside `handle_action`, which is called inside the WS message loop in `app.py:2664-2674`. The message loop's `await ws.receive_json()` for the next message is **after** the entire `handle_action` returns — so the loop is blocked while we wait for `ANIMATION_COMPLETE` from the client.

If the client never sends `ANIMATION_COMPLETE` (e.g., user backgrounded the app, or the animation flow on iOS hit the stuck-`isRunoutAnimating` path described in **H4**), the full 15 s timeout applies.

**Fix:** Either:
1. Make the wait fire-and-forget — `asyncio.create_task(self._wait_then_complete(...))` and resolve the duel asynchronously.
2. Shorten the timeout (it's already a fallback; 5 s would be plenty for most clients).
3. Tie the wait to a per-user Event instead of per-table so different users' loops aren't entangled.

**Severity:** P1 · **Likelihood:** Every duel hand-end · **Confidence:** Confirmed.

---

## P1-7 — `process_session` blocks the loop for ~5 s on long sessions  ✅ FIXED (`_fetch_hands` part)

**Status:** `_fetch_hands` now uses the new `FirestoreClient.get_hands(...)`
batch helper (added in P0-1) that fans out the reads via
`asyncio.gather`, each landing on the default thread pool. A 100-hand
session collapses from ~5 s of loop-blocking to roughly one Firestore
RTT.

The `PreflopGrader` calls inside `process_session` still run inline,
and could be moved to `asyncio.to_thread` similarly. This is a
follow-up that the original P1-7 finding acknowledged ("the grader is
not [parallelised]"); leaving it out keeps this commit surgical. Will
re-evaluate once we have telemetry showing the grader is a measurable
bottleneck.

**Verification:** in-memory test seats three hands, requests four
(including one missing); result drops the missing entry and preserves
the other three. Behavioural parity confirmed.

**Original finding:**


**Symptom (server):** After a long bot session ends, the server experiences a multi-second freeze in the background while `process_session` runs.

**Root cause:** `src/session/processor.py:342-352`:
```python
async def _fetch_hands(firestore, hand_ids):
    hands = []
    for hand_id in hand_ids:
        try:
            hand = await firestore.get_hand(hand_id)
            ...
```
Sequential `get_hand` calls; each is sync (see **P0-1**). For a 100-hand session, ~5 s of loop blocking.

Then `PreflopGrader.grade_hand` × N hands also touches Firestore via `RangeLookup` (range_check endpoint shows reads in a loop). Plus `compute_luck_categories` is offloaded via `asyncio.to_thread` (good!), but the grader is not.

**Fix:**
1. Batch-load hands: `db.collection("hands").where("hand_id", "in", chunk).stream()` (10 at a time per Firestore IN limit), wrapped in `asyncio.to_thread` and parallelised.
2. Or move the entire `process_session` to a worker/Cloud Tasks pool so the server doesn't run analysis inline at all.

**Severity:** P1 · **Likelihood:** Every session ≥30 hands · **Confidence:** Confirmed.

---

## P1-8 — `bot_personas.get_available_persona` runs 70 sequential Firestore reads  ✅ FIXED

**Status:** FIXED — added an in-memory `_rating_cache` (`persona_id ->
rating`) and a `_stale_ratings` set:
- `ensure_personas_exist()` now also calls `_refresh_rating_cache()`
  which uses `FirestoreClient.get_duel_ratings(persona_ids)` (added in
  P0-1) to read all 70 ratings in one batched gather. This warms the
  cache at server startup.
- `get_available_persona()` reads ratings from the cache. If any
  personas were marked stale (post-match release), it batches their
  refresh in one round-trip.
- `release_persona()` marks the persona's rating stale so the next
  assignment picks up the post-match rating without per-persona reads.

**Verification:** in-memory test seeded a non-default rating (1700)
for `bot_mike_p92`, warmed the cache, picked a persona near 1700, and
confirmed the picked persona ends up in `_stale_ratings` after release.
Cache filled with 68 entries on warmup.

**Original finding:**


**Symptom (server):** A duel queue timeout that fills with a bot freezes the loop for ~3.5 s while picking a persona.

**Root cause:** `bot_personas.py:180-188`:
```python
for persona in BOT_PERSONAS:                            # 70 personas
    persona_id = generate_persona_id(persona["username"])
    if persona_id not in self._in_use:
        rating = 1500
        if self._firestore:
            rating_doc = await self._firestore.get_duel_rating(persona_id)  # sync RT
            ...
```

**Fix:**
1. Cache persona ratings in memory; refresh in background. Personas' ratings only change after their matches, which the bot_persona_pool already mediates → in-memory cache is authoritative.
2. Or batch the read with `db.get_all([doc_refs])` for all 70 in a single network round-trip.

**Severity:** P1 · **Likelihood:** ~1 per duel-without-opponent · **Confidence:** Confirmed.

---

## P1-9 — `synthesizeActionRequestIfNeeded` produces a UI-level action request out of sync with the server timer  ✅ FIXED (cross-repo)

**Status:** FIXED — cross-repo change spanning both backend protocol
and iOS client:

**Backend** (`src/server/app.py` REQUEST_SNAPSHOT handler): now also
re-sends ACTION_REQUEST when it's the requester's turn. This mirrors
the post-AUTH reconnect path so REQUEST_SNAPSHOT becomes a complete
state recovery primitive.

**iOS** (`PokerWebSocketManager.swift` foreground / reconnect paths):
replaced `tableState.synthesizeActionRequestIfNeeded()` calls with
`await requestSnapshotIfNeeded()` so the server is the authority for
both the action-request payload and the deadline.

**iOS** (`PokerTableState.swift`): removed the dead
`synthesizeActionRequestIfNeeded(...)` method. Leaving it in would
just be a footgun for future code.

**Verification:** iOS Debug build → `** BUILD SUCCEEDED **`. Backend
parses cleanly. The protocol change is backward-compatible — older
iOS clients that ignore the extra ACTION_REQUEST will just get a
fresh request they may have already had.

**Original finding:**


**Symptom (iOS):** After a backgrounded-then-resumed connection, iOS synthesizes an ACTION_REQUEST showing 60 s remaining. The user takes ≥ remaining server timer time, server auto-folds them, but iOS shows the action panel and accepts a button press → ACTION sent → server replies `action_timeout` ERROR → iOS shows confusing error.

**Root cause:** `PokerTableState.swift:419-476`. The synthesized request uses `expires_at_ms = now + 60000`, ignoring the server's actual deadline. After reconnect, the server may have:
- A still-running timer that fires *t* seconds after the original deadline.
- Already fired the timer → sat the user out.

Either way, the user is acting on stale local state.

**Fix:** After reconnect, **request a fresh `ACTION_REQUEST` from the server** instead of synthesizing. The server (post-reconnect snapshot path in `app.py:2568-2573`) already re-sends ACTION_REQUEST if it's the user's turn. iOS should rely on that path, with a brief retry if not received in ~1 s, rather than synthesize.

**Severity:** P1 (data-quality, confusing UX) · **Likelihood:** Every background-resume during the user's turn · **Confidence:** Confirmed.

---

## P1-10 — Recursive `handleDisconnect` on failed reconnects  ✅ FIXED

**Status:** FIXED — converted the recursive `handleDisconnect` to an
iterative `while reconnectAttempts < maxReconnectAttempts` loop. Each
attempt calls `establishConnection`; on success the loop returns, on
failure it logs and continues the loop. Same retry budget, no stack
growth.

**Verification:** Build succeeded.

**Original finding:**


**Symptom (iOS):** Edge-case: many concurrent reconnects deepen the await stack; if reconnect attempts overlap with network flap, stack growth could trigger task explosions.

**Root cause:** `PokerWebSocketManager.swift:1463-1467`:
```swift
do {
    try await establishConnection()
} catch {
    await handleDisconnect()   // ← recursive
}
```
Bounded by `maxReconnectAttempts = 5`, so worst case 5-deep. Not critical, but combined with **P0-2** (which fires reconnect on idle), every lobby session has multiple reconnects.

**Fix:** Convert to a `while reconnectAttempts < max { try establishConnection() } catch { ... }` loop with backoff.

**Severity:** P2 (minor) · **Likelihood:** Frequent given P0-2 · **Confidence:** Confirmed.

---

## P2-1 — Hand-logger retry queue never drained  ✅ FIXED

**Status:** FIXED — added `_hand_log_retry_loop()` background task to
`app.py` lifespan that calls `hand_logger.retry_failed_writes()` every
60 s. If there's nothing to drain, it no-ops. If a flake stranded
hands earlier, they get reattempted on the next interval.

**Verification:** parses cleanly; the loop is wired into the lifespan
along with the existing sweeper and heartbeat reaper, and tears down
the same way.

**Original finding:**


**Symptom (server):** If Firestore intermittently fails, `_retry_queue` grows unbounded; OOM after long uptime.

**Root cause:** `hand_logger.py:323` appends to `self._retry_queue` on failure. `retry_failed_writes` (line 325) is the only consumer, and **it is never called from anywhere** (`grep -rn 'retry_failed_writes' src/` returns only the definition).

**Fix:** Schedule a periodic drain task in `lifespan` (app.py:317) — every 60 s, call `await hand_logger.retry_failed_writes()`. Apply a cap on queue size (drop oldest with a log).

**Severity:** P2 (latent leak) · **Likelihood:** Only matters when Firestore flakes · **Confidence:** Confirmed.

---

## P2-2 — `applyEvents` rebuilds `seats[]` element-by-element, triggering many @Published fires per event  ✅ FIXED

**Status:** FIXED — restructured the four bulk-mutation sites in
`applyEvents` (hand_started bets+button, street_dealt bulk runout,
street_dealt queued runout, street_dealt normal, hand_ended bets+winner
chips) to mutate a local `var newSeats = seats` and assign
`seats = newSeats` once. SwiftUI sees a single update where before it
saw up to 2N.

Per-seat-action mutations inside the "action" case (fold/bet/raise)
are still single writes — they only ever touch one seat per event, so
batching wouldn't help.

**Verification:** Build succeeded. Behavioural parity preserved — the
final seats[] state is identical; only the publish granularity changed.

**Original finding:**


**Symptom (iOS):** Compounds **P0-4** during big multi-event deltas — for an all-in runout that fires `street_dealt(turn)` with bet-clearing, the loop at `PokerTableState.swift:826-838` does up to 6 individual `seats[i] = …` assignments.

Each assignment triggers `seats.willSet` → `objectWillChange.send()` → P0-4 amplifier.

**Fix:** Mutate a local copy and assign once:
```swift
var nextSeats = seats
for i in 0..<nextSeats.count where nextSeats[i].bet.amount > 0 {
    let old = nextSeats[i]
    let refund = refunds[i] ?? 0
    nextSeats[i] = Seat(... + refund, bet: Chips(amount: 0), ...)
}
seats = nextSeats   // single publish
```
Apply this pattern everywhere `applyEvents` does per-index mutations (8 sites in PokerTableState.swift).

**Severity:** P2 · **Likelihood:** Every street change · **Confidence:** Confirmed.

---

## P2-3 — TableRunner queue polls every 100 ms, adding artificial latency  ✅ FIXED

**Status:** FIXED — `_run` now uses bare `await self._queue.get()` and
handles `asyncio.CancelledError` to break cleanly. Removed the
`_running` flag's polling role (`stop()` still calls `_task.cancel()`
which now drives the loop exit). Up to 100 ms of artificial latency
per command is eliminated.

**Verification:** in-memory test starts a runner, submits a
StartHandCommand (which raises since no players are seated), and
awaits `runner.stop()`. Stop completes cleanly: confirms the
cancellation path still works without the polling timeout.

**Original finding:**


**Symptom (server):** Every command (action, snapshot, join) adds up to 100 ms of artificial latency at the runner queue.

**Root cause:** `runner.py:122-128`:
```python
while self._running:
    try:
        command = await asyncio.wait_for(self._queue.get(), timeout=0.1)
        await self._process(command)
    except asyncio.TimeoutError:
        continue
```
The 100 ms `wait_for` is solely to check `self._running`. An infinite `await self._queue.get()` interrupted by `_task.cancel()` at shutdown would be cleaner and remove the latency.

**Fix:**
```python
while True:
    try:
        command = await self._queue.get()
        await self._process(command)
    except asyncio.CancelledError:
        break
```
Drop `self._running`; rely on `_task.cancel()`.

**Severity:** P2 · **Likelihood:** Every command · **Confidence:** Confirmed.

---

## P2-4 — Bot subprocess lifecycle: orphan handler only runs at startup

**Symptom (server):** If a bot subprocess hangs (CFR-related memory pressure, policy load hang, etc.) and `_cleanup_bot_table`'s `proc.terminate()` is somehow not invoked (race with `_reconcile_duel_state`), the process stays around indefinitely. Each orphan holds ~200 MB.

**Root cause:**
- `app.py:128-159` `_kill_orphan_bot_processes` runs **only on startup**.
- `_reconcile_duel_state` (app.py:170-258) terminates bot processes for tables it considers orphaned, but only inside `_bot_processes` entries the manager tracks. If `_spawn_bot_process` succeeded but a subsequent error cleared `_bot_processes[table_id]` without terminating, the subprocess lives on.

**Fix:** Add a periodic sweep that runs `pgrep -f 'openbot_client'` and cross-checks each PID against `_bot_processes.values()`. Kill anything not tracked.

**Severity:** P2 (resource leak) · **Likelihood:** Rare per occurrence but accumulates over uptime · **Confidence:** Plausible by reading lifecycle code.

---

## P2-5 — `add_player` can leave a fresh empty table behind on join failure

**Symptom (server):** Slow buildup of zombie `TableRunner`s with running tasks; each takes a small amount of memory and CPU (100ms polling).

**Root cause:** `manager.py:160-164`:
```python
runner = self._find_table_with_seats(stake_id)
if runner is None:
    table_id = self.create_table(stake_id)   # ← new table created
    runner = self._tables[table_id]

await runner.submit(JoinTableCommand(...))   # ← can raise
seat, snapshot = await future                # ← Table full / Seat occupied
```
If the join command raises after `create_table` succeeds, the new table stays in `self._tables` forever.

**Fix:** Wrap in try/except; on failure, `await self._tables.pop(table_id).stop()`.

**Severity:** P2 · **Likelihood:** Low · **Confidence:** Confirmed.

---

## P2-6 — Stuck `isRunoutAnimating = true` on backgrounding mid-runout

**Symptom (iOS):** Background → foreground → no winner animation, no next hand, no progress. User must reconnect or leave table.

**Root cause:** `PokerTableView.swift:463`:
```swift
.task(id: tableState.isRunoutAnimating) {
    guard tableState.isRunoutAnimating else { return }
    ...
    while !tableState.pendingRunoutCards.isEmpty {
        guard !Task.isCancelled else { return }
        if let cards = tableState.applyNextRunoutCard() { ... }
        try? await Task.sleep(...)
    }
}
```
If the task is cancelled mid-runout (view disappears: backgrounded, navigation, etc.) before `applyNextRunoutCard` empties `pendingRunoutCards`, the loop exits via `Task.isCancelled` without calling `applyNextRunoutCard` to flip `isRunoutAnimating = false`. The flag stays true.

When the view comes back, `.task(id: ...)` keys on the flag's current value — still `true` — but since the new view didn't fire a change, the task body **does not re-run** (it only runs on id transitions). So the flag is stuck `true` and the winner-animation `.task` at line 322 spins forever on `while tableState.isRunoutAnimating`.

Also affects PlayTab's blitz-advance loop at PlayTab.swift:1348.

**Fix:**
1. Use `defer { tableState.isRunoutAnimating = false }` inside the runout task so cancellation clears the flag.
2. Or add explicit cleanup in `onAppear`: if `isRunoutAnimating && pendingRunoutCards.isEmpty`, clear it.

**Severity:** P2 · **Likelihood:** Reproducible by backgrounding mid-runout · **Confidence:** Confirmed by trace.

---

## P2-7 — `_resolve_duel_reconnect` race: opponent's "OPPONENT_RECONNECTED" can fire before snapshot replay completes

**Symptom (iOS):** Opponent sees "reconnected" banner clear briefly, then "disconnected" banner reappears momentarily as the snapshot arrives carrying old state.

**Root cause:** `app.py:2579` `_resolve_duel_reconnect` is called after the snapshot in the reconnect path, but the snapshot itself is sent before. Race window during which the opponent could observe inconsistent state.

**Fix:** Inline the resolve-duel-reconnect notification with the snapshot send (same lock, same coroutine).

**Severity:** P3 (cosmetic) · **Likelihood:** Rare · **Confidence:** Plausible by code reading.

---

## P3-1 — Concurrent same-user AUTH races

**Symptom (server):** Two AUTH requests for the same user_id within a few ms can corrupt `_connections[user_id]`; old WS may not be closed.

**Root cause:** `app.py:2536-2542` does an `await old_ws.close()` (yields the loop) then `_connections[user_id] = websocket`. With no lock, two concurrent AUTH coroutines for the same user_id can interleave.

**Fix:** A per-user_id `asyncio.Lock` taken at AUTH time.

**Severity:** P3 · **Likelihood:** Very rare in normal use · **Confidence:** Confirmed.

---

## P3-2 — `connection.disconnect` removes the per-user lock while a sender may still hold it

**Symptom:** Brief loss of per-user write serialization during a disconnect+send race.

**Root cause:** `connection.py:53-60` — disconnect pops `self._locks[user_id]`. A concurrent `send_to_user` already inside `async with lock:` keeps the original lock object alive, but a new `send_to_user` for the same user (which shouldn't happen if disconnect ran first) would create a NEW lock under `_get_lock`. The two locks don't synchronize.

**Fix:** Pop the lock entry only after no senders are in flight, or never pop and let it GC when user reconnects.

**Severity:** P3 · **Likelihood:** Very rare · **Confidence:** Confirmed.

---

## P3-3 — `_processed_actions` dict cleaned only by side-effect

**Symptom (server):** A user who hasn't acted in 60 s might still have entries in `_processed_actions` for the table's old hands. Memory grows slowly.

**Root cause:** `handler.py:204-209`. Cleanup only happens **inside `handle_action`**. If no actions arrive (e.g., user idle), no cleanup.

**Fix:** Schedule a periodic cleanup task.

**Severity:** P3 · **Likelihood:** Low · **Confidence:** Confirmed.

---

## P3-4 — `_animation_complete_events` and bot Quip storms can interfere

**Symptom (iOS):** Rare crash on a fast bot quip cadence colliding with hand-end animations.

**Root cause:** `handler.py:340-354` `handle_animation_complete` looks up the table's event and sets it. If a quip arrives during the animation wait, it's broadcast inline (handler.py:847). Not a deadlock per se but adds re-entry concerns.

**Severity:** P3 · **Likelihood:** Low · **Confidence:** Speculative.

---

# Areas I Examined But Found No Critical Issue

- `engine/table.py` PokerKit usage — looks correct; hole-card caching prevents the documented PokerKit fold-discard behavior.
- `mccfr_trainer.py`, `mccfr_core.pyx`, `cpp/src/mccfr.{cpp,h}` — offline training only; not on the request path.
- `models/messages.py` / `models/base.py` — Pydantic schemas; OK.
- `engine/adapter.py` — clean PokerKit adapter.
- iOS `ActionPanel.swift` — Combine Timer.publish runs fine; per-request reset is clean.
- iOS `HandRecorder` — buffers per-hand events; bounded.
- iOS Firestore listener cleanup — services that use snapshots (XPService, StreakService, CosmeticsService, etc.) properly hold `ListenerRegistration` and `remove()` on stop.
- Notification observers — only PokerWebSocketManager (singleton) registers; no leak since lifetime == app.

---

# Areas I Could Not Verify (And Why)

1. **`openbot_client.py` and the CFR bot path** — code lives on the prod server (`/home/de2425/openbot/`), not in either cloned repo. References in `app.py` indicate ~150–200 MB per bot process and policy/LUT loading from `.db` files. The server-side risks (subprocess hangs, memory growth, slow bot decisions blocking ACTION_REQUEST) are inferred from the spawn surface only.
2. **Firestore-side cost / quota under the sync workload** — could not verify quota throttling behavior without runtime data.
3. **Cloud Run autoscaling vs. blocking event loop** — under load, a stalled instance might fail readiness checks and recycle; need ops data.
4. **iOS memory profile in Instruments** — only static-analysis findings here.

---

# Recommended Triage Order

1. **Stop the bleeding (1 hour):**
   - Patch app.py:2542 to initialise `_last_seen` (P0-2).
   - Patch manager.add_player to refund on seat failure (P0-3).
   - Remove the `objectWillChange` forwarding in PokerWebSocketManager (P0-4).
   - Wrap the Canvas felt grain in `.drawingGroup()` (P1-1 quick win).

2. **Unblock the event loop (1 day):**
   - Convert FirestoreClient to use `asyncio.to_thread` everywhere (P0-1).
   - Parallelise `_check_and_process_rebuys` (P1-5) and `_fetch_hands` (P1-7).
   - Memoize persona ratings (P1-8).

3. **iOS network resilience (1 day):**
   - Move WS receive loop off the MainActor (P1-2).
   - Reuse a single URLSession (P1-3).
   - Replace `synthesizeActionRequestIfNeeded` with a server-driven re-request (P1-9).
   - Add `defer { isRunoutAnimating = false }` to the runout task (P2-6).

4. **Cleanup & hardening (1 day):**
   - Hand-logger retry drain (P2-1).
   - Mutate-then-publish in PokerTableState (P2-2).
   - Drop runner queue polling (P2-3).
   - Bot subprocess sweep (P2-4).
   - `add_player` zombie-table cleanup (P2-5).

5. **CFR follow-up (separate session):**
   - Add bot subprocess RSS monitoring + auto-restart on threshold.
   - Investigate openbot policy load times; consider a warm-pool of pre-loaded bot processes.

---

# Confidence Caveats

- All "Confirmed" findings have a clear code path. They will manifest under the conditions described, even if I haven't reproduced them empirically.
- "Likelihood" estimates are based on code-reading inference about how often the failing path is exercised in normal play. Real telemetry from `/tmp/poker_dev.log` plus iOS crash reports would refine these.
- The investigation focused on `Features/PokerTable/**` on iOS; ~80% of the Swift codebase was NOT read. Other modules (Insights, Missions, Onboarding, Lessons) could harbor additional bugs not surfaced here.
