# Retrieval Evaluation Report

Generated: 2026-04-07T22:46:19.311498

## Summary

| Metric | Value |
|--------|-------|
| Total scenarios | 20 |
| Top-1 accuracy | 80.0% |
| Top-3 accuracy | 100.0% |

## By Street

| Street | Top-1 | Top-3 | N |
|--------|-------|-------|---|
| preflop | 60.0% | 100.0% | 5 |
| flop | 80.0% | 100.0% | 5 |
| turn | 80.0% | 100.0% | 5 |
| river | 100.0% | 100.0% | 5 |

## Detailed Results

### ✓ preflop-1
- **Street:** preflop
- **Query:** 3-betting from big blind against button open preflop ranges
- **Filters:** {'streets': ['preflop'], 'pot_types': ['3-bet'], 'positions': ['OOP']}
- **Expected:** ['janda-p2-003', 'janda-p2-004', 'janda-p2-011']
- **Retrieved:** ['janda-p13-007', 'janda-p2-025', 'janda-p2-019']
- **Scores:** ['0.884', '0.883', '0.880']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ preflop-2
- **Street:** preflop
- **Query:** defending against 3-bet preflop calling vs 4-betting range construction
- **Filters:** {'streets': ['preflop'], 'pot_types': ['3-bet'], 'positions': ['IP']}
- **Expected:** ['janda-p2-017', 'janda-p2-018', 'janda-p2-013']
- **Retrieved:** ['janda-p2-020', 'janda-p2-017', 'janda-p2-024']
- **Scores:** ['0.910', '0.905', '0.900']
- **Top-1 correct:** False
- **Top-3 correct:** True

### ✓ preflop-3
- **Street:** preflop
- **Query:** facing 4-bet preflop strategy
- **Filters:** {'streets': ['preflop'], 'pot_types': ['4-bet'], 'positions': ['IP']}
- **Expected:** ['janda-p2-020', 'janda-p2-021', 'janda-p2-007']
- **Retrieved:** ['janda-p2-020', 'janda-p2-005', 'janda-p2-021']
- **Scores:** ['0.887', '0.880', '0.876']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ preflop-4
- **Street:** preflop
- **Query:** cold calling open raise button position preflop
- **Filters:** {'streets': ['preflop'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p2-023', 'janda-p2-014', 'janda-p1-003']
- **Retrieved:** ['janda-p16-026', 'janda-p6-010', 'janda-p13-007']
- **Scores:** ['0.854', '0.851', '0.849']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ preflop-5
- **Street:** preflop
- **Query:** opening range UTG position first to act
- **Filters:** {'streets': ['preflop'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p2-003', 'janda-p2-023', 'janda-p2-025']
- **Retrieved:** ['janda-p2-004', 'janda-p2-003', 'janda-p2-010']
- **Scores:** ['0.836', '0.831', '0.829']
- **Top-1 correct:** False
- **Top-3 correct:** True

### ✓ flop-1
- **Street:** flop
- **Query:** c-betting dry rainbow flop in position single raised pot
- **Filters:** {'streets': ['flop'], 'board_textures': ['rainbow', 'disconnected', 'dry'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p5-004', 'janda-p5-005', 'janda-p5-001']
- **Retrieved:** ['janda-p8-005', 'janda-p15-002', 'janda-p6-005']
- **Scores:** ['0.828', '0.826', '0.825']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ flop-2
- **Street:** flop
- **Query:** defending flop c-bet wet monotone board out of position
- **Filters:** {'streets': ['flop'], 'board_textures': ['monotone', 'wet', 'high'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p4-024', 'janda-p4-017', 'janda-p6-005']
- **Retrieved:** ['janda-p6-027', 'janda-p6-005', 'janda-p5-012']
- **Scores:** ['0.849', '0.846', '0.845']
- **Top-1 correct:** False
- **Top-3 correct:** True

### ✓ flop-3
- **Street:** flop
- **Query:** check-raise strategy connected wet flop OOP
- **Filters:** {'streets': ['flop'], 'board_textures': ['two_tone', 'connected', 'wet'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p6-017', 'janda-p6-18', 'janda-p6-19']
- **Retrieved:** ['janda-p6-017', 'janda-p6-029', 'janda-p5-012']
- **Scores:** ['0.869', '0.856', '0.854']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ flop-4
- **Street:** flop
- **Query:** multiway pot flop play first to act
- **Filters:** {'streets': ['flop'], 'board_textures': ['rainbow', 'disconnected', 'dry'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p12-001', 'janda-p12-002', 'janda-p12-007']
- **Retrieved:** ['janda-p12-001', 'janda-p12-007', 'janda-p12-003']
- **Scores:** ['0.860', '0.850', '0.838']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ flop-5
- **Street:** flop
- **Query:** 3-bet pot flop c-bet high board in position
- **Filters:** {'streets': ['flop'], 'board_textures': ['rainbow', 'dry', 'high'], 'pot_types': ['3-bet'], 'positions': ['IP']}
- **Expected:** ['janda-p7-001', 'janda-p7-002', 'janda-p7-004']
- **Retrieved:** ['janda-p7-008', 'janda-p7-007', 'janda-p10-001']
- **Scores:** ['0.856', '0.850', '0.841']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ turn-1
- **Street:** turn
- **Query:** delayed c-bet turn after checking back flop
- **Filters:** {'streets': ['turn'], 'board_textures': ['rainbow', 'dry'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p8-017', 'janda-p8-005', 'janda-p8-011']
- **Retrieved:** ['janda-p8-017', 'janda-p10-005', 'janda-p9-011']
- **Scores:** ['0.868', '0.857', '0.856']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ turn-2
- **Street:** turn
- **Query:** double barrel turn wet board flush completing
- **Filters:** {'streets': ['turn'], 'board_textures': ['two_tone', 'wet'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p8-012', 'janda-p8-006', 'janda-p10-002']
- **Retrieved:** ['janda-p10-002', 'janda-p9-017', 'janda-p8-010']
- **Scores:** ['0.827', '0.822', '0.818']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ turn-3
- **Street:** turn
- **Query:** check-raise turn out of position after calling flop
- **Filters:** {'streets': ['turn'], 'board_textures': ['rainbow', 'disconnected', 'dry'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p9-016', 'janda-p9-020', 'janda-p9-018']
- **Retrieved:** ['janda-p9-018', 'janda-p8-011', 'janda-p6-029']
- **Scores:** ['0.881', '0.878', '0.875']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ turn-4
- **Street:** turn
- **Query:** 3-bet pot turn defense facing bet OOP
- **Filters:** {'streets': ['turn'], 'board_textures': ['rainbow', 'disconnected', 'dry', 'high'], 'pot_types': ['3-bet'], 'positions': ['OOP']}
- **Expected:** ['janda-p10-007', 'janda-p10-002', 'janda-p10-001']
- **Retrieved:** ['janda-p7-003', 'janda-p10-007', 'janda-p7-008']
- **Scores:** ['0.864', '0.858', '0.857']
- **Top-1 correct:** False
- **Top-3 correct:** True

### ✓ turn-5
- **Street:** turn
- **Query:** turn jam all-in decision with draw connected board
- **Filters:** {'streets': ['turn'], 'board_textures': ['rainbow', 'dry'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p8-019', 'janda-p9-017', 'janda-p10-008']
- **Retrieved:** ['janda-p10-003', 'janda-p8-018', 'janda-p10-011']
- **Scores:** ['0.847', '0.839', '0.838']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ river-1
- **Street:** river
- **Query:** river value betting in position
- **Filters:** {'streets': ['river'], 'board_textures': ['rainbow', 'disconnected', 'dry'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p11-002', 'janda-p11-003', 'janda-p11-008']
- **Retrieved:** ['janda-p11-002', 'janda-p11-006', 'janda-p11-004']
- **Scores:** ['0.903', '0.877', '0.875']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ river-2
- **Street:** river
- **Query:** river bluff catching calling decision facing bet
- **Filters:** {'streets': ['river'], 'board_textures': ['rainbow', 'high'], 'pot_types': ['single_raised'], 'positions': ['OOP']}
- **Expected:** ['janda-p11-013', 'janda-p11-014', 'janda-p1-010']
- **Retrieved:** ['janda-p1-010', 'janda-p10-011', 'janda-p10-012']
- **Scores:** ['0.838', '0.834', '0.833']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ river-3
- **Street:** river
- **Query:** thin value bet river marginal hand
- **Filters:** {'streets': ['river'], 'board_textures': ['rainbow', 'disconnected', 'dry'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p11-003', 'janda-p11-006', 'janda-p11-007']
- **Retrieved:** ['janda-p9-009', 'janda-p5-008', 'janda-p11-002']
- **Scores:** ['0.852', '0.851', '0.849']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ river-4
- **Street:** river
- **Query:** river overbet sizing polarized range
- **Filters:** {'streets': ['river'], 'board_textures': ['rainbow'], 'pot_types': ['single_raised'], 'positions': ['IP']}
- **Expected:** ['janda-p11-017', 'janda-p11-008', 'janda-p3-002']
- **Retrieved:** ['janda-p3-002', 'janda-p14-004', 'janda-p3-003']
- **Scores:** ['0.884', '0.883', '0.881']
- **Top-1 correct:** True
- **Top-3 correct:** True

### ✓ river-5
- **Street:** river
- **Query:** converting made hand to bluff river 3-bet pot
- **Filters:** {'streets': ['river'], 'board_textures': ['rainbow', 'high'], 'pot_types': ['3-bet'], 'positions': ['OOP']}
- **Expected:** ['janda-p11-016', 'janda-p11-015', 'janda-p7-008']
- **Retrieved:** ['janda-p11-016', 'janda-p4-010', 'janda-p4-008']
- **Scores:** ['0.876', '0.873', '0.867']
- **Top-1 correct:** True
- **Top-3 correct:** True

## Tuning Notes

If accuracy is below 70%, consider:

1. **Hybrid search** - Combine BM25 keyword search with semantic search
2. **Query expansion** - Generate 2-3 variant queries and merge results
3. **Adjust embedding text** - Include more/less context in chunk embeddings
4. **Metadata boost** - Weight filtered results higher in scoring
5. **Rerank** - Use a cross-encoder reranker on top-N results
