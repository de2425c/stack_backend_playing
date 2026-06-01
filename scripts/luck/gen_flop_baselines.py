"""Generate per-class flop baselines for the flop run-good luck category.

For each of the 169 starting-hand classes, computes:
  - flop_equity_var:   Var_flop[ E_villain[ equity | hero=c, flop, random villain ] ]
  - p_set_plus:        prob hero flops a set/trips or better (incl straight, flush)
  - p_two_pair_plus:   prob hero flops two-pair-or-better (uses ≥1 hero card)
  - p_pair_plus:       prob hero flops a pair-or-better using a hero card
  - p_flush_draw:      prob hero flops a flush draw (4-of-a-suit, hero contributes)
  - p_oesd:            prob hero flops an open-ended straight draw
  - p_strong_draw:     prob hero flops a flush draw or OESD

Mean of flop equity per class equals the precomputed preflop equity vs random
(law of total expectation) — we don't re-store it here.

Writes src/insights/luck/data/flop_baselines.json.
"""

from __future__ import annotations
import json
import random
import time
from pathlib import Path

from pokerkit import StandardHighHand, Card

# Allow `from src...` style imports if needed, otherwise just inline the classifier.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.insights.luck.categories.flop import classify_flop  # noqa: E402


RANKS = "AKQJT98765432"
SUITS = "hdcs"


def canonical_combo(hand_class: str) -> list[Card]:
    if len(hand_class) == 2:
        r = hand_class[0]
        return list(Card.parse(f"{r}s{r}h"))
    r1, r2, kind = hand_class[0], hand_class[1], hand_class[2]
    if kind == "s":
        return list(Card.parse(f"{r1}s{r2}s"))
    return list(Card.parse(f"{r1}s{r2}h"))


def canonical_strs(hand_class: str) -> list[str]:
    if len(hand_class) == 2:
        r = hand_class[0]
        return [f"{r}s", f"{r}h"]
    r1, r2, kind = hand_class[0], hand_class[1], hand_class[2]
    if kind == "s":
        return [f"{r1}s", f"{r2}s"]
    return [f"{r1}s", f"{r2}h"]


def all_classes() -> list[str]:
    out = []
    seen = set()
    for i, r1 in enumerate(RANKS):
        for j, r2 in enumerate(RANKS):
            if i == j:
                c = r1 + r2
            elif i < j:
                c = r1 + r2 + "s"
            else:
                c = r2 + r1 + "o"
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _eval_one(hero_cards: list[Card], villain: list[Card], full_board: list[Card]) -> float:
    h = StandardHighHand.from_game(hero_cards, full_board)
    v = StandardHighHand.from_game(villain, full_board)
    if h > v:
        return 1.0
    if h == v:
        return 0.5
    return 0.0


def compute_class_stats(
    hand_class: str,
    n_flops: int,
    n_villain_per_flop: int,
    rng: random.Random,
) -> dict:
    hero_cards = canonical_combo(hand_class)
    hero_strs = canonical_strs(hand_class)
    full_deck = [Card(r, s) for r in RANKS for s in SUITS]
    remaining_after_hero = [c for c in full_deck if c not in hero_cards]

    flop_equities: list[float] = []
    counts = {
        "set_plus": 0,
        "two_pair_plus": 0,
        "pair_plus": 0,
        "flush_draw": 0,
        "oesd": 0,
        "strong_draw": 0,
    }

    for _ in range(n_flops):
        flop = rng.sample(remaining_after_hero, 3)
        flop_strs = [f"{c.rank}{c.suit}" for c in flop]
        remaining_after_flop = [c for c in remaining_after_hero if c not in flop]

        # Inner MC: sample (villain, turn, river) and average hero showdown share.
        wins = 0.0
        for _v in range(n_villain_per_flop):
            sample = rng.sample(remaining_after_flop, 4)
            villain = sample[:2]
            full_board = flop + sample[2:]
            wins += _eval_one(hero_cards, villain, full_board)
        flop_eq = wins / n_villain_per_flop
        flop_equities.append(flop_eq)

        cats = classify_flop(hero_strs, flop_strs)
        for k in counts:
            if cats[k]:
                counts[k] += 1

    # Sample variance of flop-equity estimator; slightly biased upward by inner MC noise.
    mean = sum(flop_equities) / len(flop_equities)
    var = sum((e - mean) ** 2 for e in flop_equities) / max(1, len(flop_equities) - 1)

    out = {"flop_equity_var": round(var, 6), "flop_equity_mean_sampled": round(mean, 4)}
    for k, c in counts.items():
        out[f"p_{k}"] = round(c / n_flops, 4)
    return out


def main(n_flops: int = 500, n_villain: int = 100, seed: int = 42) -> None:
    out_path = (
        Path(__file__).resolve().parents[2]
        / "src/insights/luck/data/flop_baselines.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    classes = all_classes()
    assert len(classes) == 169, len(classes)

    print(f"[gen_flop_baselines] {len(classes)} classes, n_flops={n_flops}, n_villain={n_villain}")
    t0 = time.time()
    results: dict[str, dict] = {}
    for i, c in enumerate(classes):
        ts = time.time()
        results[c] = compute_class_stats(c, n_flops, n_villain, rng)
        dt = time.time() - ts
        elapsed = time.time() - t0
        eta = elapsed / (i + 1) * (len(classes) - i - 1)
        print(f"  [{i+1}/{len(classes)}] {c:>4s}  var={results[c]['flop_equity_var']:.4f}  "
              f"mean={results[c]['flop_equity_mean_sampled']:.3f}  "
              f"({dt:.1f}s, eta {eta/60:.1f}m)")

    payload = {
        "n_flops_per_class": n_flops,
        "n_villain_iters_per_flop": n_villain,
        "classes": results,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[gen_flop_baselines] wrote {out_path} in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--n-flops", type=int, default=500)
    p.add_argument("--n-villain", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    main(args.n_flops, args.n_villain, args.seed)
