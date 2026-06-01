# iOS HU (Heads-Up) Integration Plan

**Created:** 2026-03-26

## Overview

Add 2-player table support to the iOS app. Backend already supports HU - this is UI/client work only.

**Key Changes:**
- Add table type selector (HU vs 6-max)
- Dynamic seat count (2 or 6)
- HU-specific table layout
- Update bot count logic

---

## Phase 1: Protocol Updates

**Goal:** Support `max_players` in messages

**Files:**
- `stackpoker/stack/Features/PokerTable/Model/PokerProtocol.swift`

### Step 1.1: Add max_players to TableSnapshotMessage
```swift
// PokerProtocol.swift - TableSnapshotMessage (line ~236)
struct TableSnapshotMessage: Decodable {
    // ... existing fields
    let max_players: Int?  // ADD - nil for backwards compat, default 6
}
```

### Step 1.2: Update CreateBotTableMessage
```swift
// PokerProtocol.swift - CreateBotTableMessage (line ~37)
struct CreateBotTableMessage: Encodable {
    let type = "CREATE_BOT_TABLE"
    let stake_id: String       // "nlh_1_2" for 6-max, "nlh_1_2_hu" for HU
    let buy_in_cents: Int
    let display_name: String
    let bot_count: Int?        // CHANGE: optional, nil = auto from max_players
    let auto_top_up: Bool
    let blitz_mode: Bool
    let persona_ids: [String]?
    let bot_ids: [String]?
}
```

---

## Phase 2: State Management

**Goal:** Handle dynamic seat count

**Files:**
- `stackpoker/stack/Features/PokerTable/Model/PokerTableState.swift`

### Step 2.1: Add maxPlayers property
```swift
// PokerTableState.swift (line ~17)
@Published var maxPlayers: Int = 6  // ADD

// In update(from snapshot:) function (line ~245)
func update(from snapshot: TableSnapshotMessage) {
    // ADD: update maxPlayers
    self.maxPlayers = snapshot.max_players ?? 6

    // CHANGE: use maxPlayers instead of hardcoded 6
    for i in 0..<maxPlayers {
        // ...
    }
}
```

---

## Phase 3: Table Layout

**Goal:** HU-specific seat positioning

**Files:**
- `stackpoker/stack/Features/PokerTable/View/PokerTableView.swift`

### Step 3.1: Update seatPosition() for HU
```swift
// PokerTableView.swift - seatPosition() (line ~837)
private func seatPosition(for seatIndex: Int, ...) -> (CGFloat, CGFloat) {
    let maxPlayers = tableState.maxPlayers

    if maxPlayers == 2 {
        // HU: Hero bottom center, opponent top center
        let adjustedIndex = (seatIndex - heroSeat + 2) % 2
        let positions: [(x: CGFloat, y: CGFloat)] = [
            (0.0, 0.0),      // Hero (bottom)
            (0.0, -0.72),    // Opponent (top)
        ]
        return positions[adjustedIndex]
    }

    // Existing 6-max logic...
}
```

---

## Phase 4: Table Type Selection UI

**Goal:** User can choose HU vs 6-max

**Files:**
- `stackpoker/stack/Features/PokerTable/View/PlayTab.swift`

### Step 4.1: Add table type state
```swift
// PlayTab.swift (add near top of file)
enum TableType: String, CaseIterable {
    case sixMax = "6-Max"
    case headsUp = "Heads-Up"

    var maxPlayers: Int {
        switch self {
        case .sixMax: return 6
        case .headsUp: return 2
        }
    }

    var stakeId: String {
        switch self {
        case .sixMax: return "nlh_1_2"
        case .headsUp: return "nlh_1_2_hu"
        }
    }

    var botCount: Int {
        return maxPlayers - 1
    }
}

// In PlayTab struct
@State private var selectedTableType: TableType = .sixMax
```

### Step 4.2: Add toggle UI
```swift
// PlayTab.swift - in form section (around line 350)
Picker("Table Type", selection: $selectedTableType) {
    ForEach(TableType.allCases, id: \.self) { type in
        Text(type.rawValue).tag(type)
    }
}
.pickerStyle(.segmented)
```

### Step 4.3: Update playAgainstBots()
```swift
// PlayTab.swift - playAgainstBots() (line ~496)
let botCount = selectedTableType.botCount
let stakeId = selectedTableType.stakeId
let bots = botPersonaService.selectBotsForTable(elo: elo, count: botCount)

wsManager.createBotTable(
    stakeId: stakeId,
    buyInCents: 20000,
    displayName: displayName,
    botCount: nil,  // Let backend auto-calculate
    // ...
)
```

---

## Phase 5: Bot Selection

**Goal:** Select correct number of bots

**Files:**
- `stackpoker/stack/Features/Play/Service/BotPersonaService.swift`
- `stackpoker/stack/Features/PokerTable/View/PlayTab.swift`

### Step 5.1: Update bot selection call
```swift
// PlayTab.swift - playAgainstBots()
let botCount = selectedTableType.botCount  // 1 for HU, 5 for 6-max
let bots = botPersonaService.selectBotsForTable(elo: elo, count: botCount)
```

### Step 5.2: Update lobby preview
```swift
// PlayTab.swift - lobbyPreviewState (line ~243)
let previewBotCount = selectedTableType.botCount
for (index, bot) in lobbyPreviewBots.prefix(previewBotCount).enumerated() {
    // ...
}
```

---

## Phase 6: Visual Polish

**Goal:** HU-specific UI adjustments

**Files:**
- `stackpoker/stack/Features/PokerTable/View/PokerTableView.swift`
- `stackpoker/stack/Features/PokerTable/View/CommunityCardsView.swift`

### Step 6.1: Adjust table felt for HU
- Consider smaller table graphic
- Center community cards appropriately
- Adjust pot display position

### Step 6.2: Update player count display
```swift
// PlayTab.swift
Text("Players: \(selectedTableType.maxPlayers) Players")
```

---

## Checklist

### Phase 1: Protocol ✅
- [x] Add `max_players` to `TableSnapshotMessage`
- [x] Make `bot_count` optional in `CreateBotTableMessage`

### Phase 2: State ✅
- [x] Add `maxPlayers` property to `PokerTableState`
- [x] Update seat array creation to use `maxPlayers`
- [x] Update `reset()` to reset `maxPlayers`
- [x] Update `updateSeat()` to validate against `maxPlayers`

### Phase 3: Layout ✅
- [x] Add HU seat positions (2-player layout)
- [x] Update `seatPosition()` to branch on `maxPlayers`
- [x] Update `dealerButtonPosition()` for HU

### Phase 4: UI Selection ✅
- [x] Create `TableType` enum
- [x] Add table type picker to PlayTab
- [x] Update `playAgainstBots()` to use selected type
- [x] Pass correct `stake_id` ("nlh_1_2_hu" for HU)

### Phase 5: Bots ✅
- [x] Update `selectBotsForTable()` call with dynamic count
- [x] Update lobby preview bot count

### Phase 6: Polish
- [ ] Test HU layout looks good
- [ ] Verify pot/cards positioning for 2-player view

---

## File Reference

| File | Purpose |
|------|---------|
| `PokerProtocol.swift` | Message structs (CreateBotTable, TableSnapshot) |
| `PokerTableState.swift` | Table state, seat management |
| `PokerTableView.swift` | Table rendering, seat positions |
| `PlayTab.swift` | Game setup UI, bot table creation |
| `BotPersonaService.swift` | Bot selection logic |
| `PokerWebSocketManager.swift` | WebSocket message sending |

---

## Testing

1. Select HU → verify 1 bot selected in preview
2. Start HU game → verify `stake_id: "nlh_1_2_hu"` sent
3. Verify table shows 2 seats (hero + 1 opponent at top)
4. Play full hand → verify blind posting, action order
5. Select 6-max → verify 5 bots, normal layout

---

## Dependencies

Backend HU support (completed):
- Phase 1-4: Backend player count handling ✅
- Phase 5-6: Bot policy routing ✅
- Phase 7: Bot spawning with auto bot_count ✅
- Phase 8: Integration tested ✅

See: `docs/HU_IMPLEMENTATION_PLAN.md` for backend details.
