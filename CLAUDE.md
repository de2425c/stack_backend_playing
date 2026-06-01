# Poker Backend

Real-time poker game server using WebSockets, PokerKit engine, and Firebase auth.

## Quick Start

All development happens on this server (`162.222.177.28`). See **Common Tasks → Restart Server** below for the full restart command with policy env vars — don't use a bare `uvicorn` invocation, it will boot with stale defaults.

**Check logs:** `tail -f /tmp/poker_dev.log` (8000) or `/tmp/poker.log` (8001)
**Kill server:** `fuser -k 8000/tcp` (or `8001/tcp`)

## Ports

| Port | Service | Description |
|------|---------|-------------|
| 8000 | Poker WebSocket | Main game server (ws://162.222.177.28:8000/ws) |
| 8001 | Analytics API | Stats/analytics (http://34.57.59.242:8001) |

## Project Structure

```
src/
├── server/          # WebSocket server, handlers, auth
│   ├── app.py       # FastAPI app, startup, timer setup
│   ├── handler.py   # Message routing, action handling
│   ├── timer.py     # Action timeout service (60s)
│   └── connections.py
├── engine/          # Game logic
│   ├── table.py     # PokerTableEngine, PokerKit adapter
│   └── config.py    # Table config (blinds, timeouts)
├── manager/         # Table management
│   ├── runner.py    # TableRunner, command processing
│   └── commands.py  # Command types (Join, Action, Timeout)
├── models/          # Pydantic schemas
│   ├── base.py      # Core types (Card, Chips, Seat, Events)
│   └── messages.py  # Protocol messages (ACTION_REQUEST, etc)
└── persistence/     # Firebase/Firestore integration
```

## Key Configs

**src/engine/config.py:**
- `action_timeout_seconds: int = 60` - Player action timeout
- `small_blind / big_blind` - Blind amounts in cents

## WebSocket Protocol

**Client -> Server:**
- `AUTH` - Authenticate with Firebase token
- `JOIN_POOL` - Join matchmaking queue
- `JOIN_TABLE` / `CREATE_BOT_TABLE` / `JOIN_DUEL` / `CANCEL_DUEL`
- `ACTION` - fold/check/call/bet/raise_to
- `LEAVE_TABLE` / `NEXT_HAND` / `ANIMATION_COMPLETE` / `TOP_UP_REQUEST` / `SET_AUTO_TOP_UP` / `QUIP` / `PING`
- `REQUEST_SNAPSHOT` - Client-driven resync. Send this when `STATE_DELTA.seq` jumps past `lastSeen + 1`. Server replies with the current `TABLE_SNAPSHOT` (fresh seq) or an `ERROR/not_at_table`. Avoids tearing down the WS just to recover from a missed delta.

**Server -> Client:**
- `AUTH_OK` - Auth successful
- `TABLE_SNAPSHOT` - Full table state (also returned by `REQUEST_SNAPSHOT`)
- `STATE_DELTA` - Game events (actions, cards dealt). Carries monotonic `seq` per table — clients **must** track `seq` from deltas (not only snapshots) and request a snapshot if a gap is detected.
- `ACTION_REQUEST` - Your turn to act (includes timer)
- `SEAT_UPDATE`, `REBUY`, `TOP_UP_PENDING`, `OUT_OF_CHIPS`, `QUIP`, `DUEL_QUEUED/MATCHED/ENDED/CANCELLED`, `TABLE_LEFT`, `PONG`, `ERROR`

## Send-side invariants

- `ConnectionManager.send_to_user(user_id, msg)` is the single chokepoint for **post-AUTH** outbound messages — it holds a per-user `asyncio.Lock` so concurrent producers (broadcast, ACTION_REQUEST send, timer firings, duel events) cannot interleave JSON frames on the wire.
- Pre-AUTH error responses (`First message must be AUTH`, `Invalid token`) call `websocket.send_json` directly because there is no concurrent producer at that point.
- New direct send sites (e.g. for new message types) MUST go through `connections.send_to_user` once the user is registered.

## Testing

```bash
pytest                           # All tests
pytest tests/test_engine.py      # Engine tests only
pytest -v                        # Verbose output
```

## Bot routing (HU duels)

`_get_policy_for_game()` in `openbot/src/translation/translator.py` selects the HU policy by effective stack:

| Effective stack | Policy file | Format | Abstraction |
|---|---|---|---|
| ≤ 11.5 bb | `OPENBOT_HU_POLICY_8BB` (`hu8bb_policy.db`) | msgpack | NHS2 |
| 11.5–30 bb | `OPENBOT_HU_POLICY_15BB` (`hu15bb_v2_policy.db`) | text (Slumbot) | NHS2 |
| 30–75 bb | `OPENBOT_HU_POLICY_50BB` (`hu50bb_policy.db`) | text (Slumbot) | NHS2 |
| > 75 bb | `OPENBOT_HU_POLICY_100BB` (`hu100bb_policy.db`) | text (Slumbot) | NHS2 |
| 6-max | `OPENBOT_POLICY` (`policy_iter252M.db`) | msgpack | openbot |

All HU tiers use NHS2 LUTs (Slumbot abstraction); 6-max uses openbot LUTs. There is no policy-routing for 6-max — a single blueprint covers all stack depths. NHS2 LUTs live at `SLUMBOT_LUT_DIR` (default `/home/de2425/nhs2_luts`); openbot LUTs live at `OPENBOT_ABSTRACTION_DIR` (must point at `/home/de2425/openbot/models/checkpoints` — pointing it at `nhs2_luts` silently breaks 6-max postflop bucketing). Push/fold ranges (`hu_pushfold_ranges.json`) override policy lookup at ≤5 bb.

The river solver is **skipped for HU** because `BlueprintRangeBuilder` seeds villain ranges from the 6-max blueprint and produces garbage strategy when invoked under HU shortstack; HU policies handle the river directly.

## Common Tasks

### Restart Server
Always pass the policy / abstraction env vars explicitly — `app.py` defaults exist but drift behind the freshest checkpoints.

```bash
fuser -k 8000/tcp; sleep 1   # or 8001 for prod
cd ~/poker_backend && source venv/bin/activate && \
  GOOGLE_APPLICATION_CREDENTIALS=/home/de2425/firebase-credentials.json \
  AUTH_ENABLED=true PORT=8000 \
  OPENBOT_POLICY=/home/de2425/policy_iter252M.db \
  OPENBOT_HU_POLICY_8BB=/home/de2425/hu8bb_policy.db \
  OPENBOT_HU_POLICY_15BB=/home/de2425/hu15bb_v2_policy.db \
  OPENBOT_HU_POLICY_50BB=/home/de2425/hu50bb_policy.db \
  OPENBOT_HU_POLICY_100BB=/home/de2425/hu100bb_policy.db \
  OPENBOT_ABSTRACTION_DIR=/home/de2425/openbot/models/checkpoints \
  nohup ./venv/bin/uvicorn src.server.app:app --host 0.0.0.0 --port 8000 > /tmp/poker_dev.log 2>&1 &
```

Run `ls -lt /home/de2425/*.db` first; some files (e.g. `hu15bb_policy.db`, `policy_iter200M.db`) are 0-byte placeholders and should not be used.

### Where code lives
All development happens directly on this server (`162.222.177.28`). There is no local/server sync — edit files in place. The old Mac checkout at `/Users/davideyal/Projects/stack_poker/` is stale and should not be rsynced from.

### Debug Timeouts
Look for these log patterns:
- `[TIMER_REG]` - Timer registered for player
- `[TIMER] Timeout expired` - Timer fired
- `[TIMEOUT] Processing` - Handling timeout action

## iOS Client Connection

The iOS app connects to: `ws://162.222.177.28:8000/ws`

Configured in: `stackpoker/stack/Features/PokerTable/Service/PokerWebSocketManager.swift`
