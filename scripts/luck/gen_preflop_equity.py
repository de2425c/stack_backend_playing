"""Generate the equity-vs-random-hand lookup table for all 169 starting-hand classes.

Writes src/insights/luck/data/preflop_equity_vs_random.json:
{
  "n_trials": 5000,
  "variance": 0.0254,  # combo-weighted Var(e_h) across the 1326 combo distribution
  "mean": 0.5000,      # combo-weighted E[e_h] (sanity check; should be ~0.5)
  "classes": {
    "AA": {"equity": 0.853, "combos": 6},
    "AKs": {"equity": 0.670, "combos": 4},
    "AKo": {"equity": 0.654, "combos": 12},
    ...
  }
}

Each class's equity is hero's pot share vs a uniformly random villain hand
across a fully-dealt board (preflop all-in equity). Computed by Monte Carlo:
for each trial, sample 2 villain cards + 5 board cards from the 50 remaining
cards (after removing hero's canonical combo), evaluate showdown.
"""

from __future__ import annotations
import json
import random
import time
from pathlib import Path

from pokerkit import StandardHighHand, Card


RANKS = "AKQJT98765432"
SUITS = "hdcs"


def canonical_combo(hand_class: str) -> list[Card]:
    """Pick a canonical 2-card combo for a class (e.g. 'AKs' -> AsKs, 'AKo' -> AsKh, 'AA' -> AsAh)."""
    if len(hand_class) == 2:  # pair
        r = hand_class[0]
        return list(Card.parse(f"{r}s{r}h"))
    r1, r2, kind = hand_class[0], hand_class[1], hand_class[2]
    if kind == "s":
        return list(Card.parse(f"{r1}s{r2}s"))
    return list(Card.parse(f"{r1}s{r2}h"))


def combos_for(hand_class: str) -> int:
    if len(hand_class) == 2:
        return 6
    return 4 if hand_class[2] == "s" else 12


def all_classes() -> list[str]:
    out = []
    for i, r1 in enumerate(RANKS):
        for j, r2 in enumerate(RANKS):
            if i == j:
                out.append(r1 + r2)  # pair
            elif i < j:
                out.append(r1 + r2 + "s")
            else:
                out.append(r2 + r1 + "o")
    # Deduplicate (pairs counted on diagonal only)
    seen = set()
    uniq = []
    for c in out:
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)
    return uniq


def equity_vs_random(hero: list[Card], n_trials: int, rng: random.Random) -> float:
    full_deck = [Card(r, s) for r in RANKS for s in SUITS]
    remaining = [c for c in full_deck if c not in hero]
    wins = 0.0
    for _ in range(n_trials):
        draw = rng.sample(remaining, 7)
        villain = draw[:2]
        board = draw[2:]
        h = StandardHighHand.from_game(hero, board)
        v = StandardHighHand.from_game(villain, board)
        if h > v:
            wins += 1.0
        elif h == v:
            wins += 0.5
    return wins / n_trials


def main(n_trials: int = 5000, seed: int = 42) -> None:
    out_path = Path(__file__).resolve().parents[2] / "src/insights/luck/data/preflop_equity_vs_random.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    classes = all_classes()
    assert len(classes) == 169, len(classes)

    rng = random.Random(seed)
    results: dict[str, dict] = {}

    t0 = time.time()
    for i, cls in enumerate(classes):
        hero = canonical_combo(cls)
        eq = equity_vs_random(hero, n_trials, rng)
        results[cls] = {"equity": round(eq, 4), "combos": combos_for(cls)}
        if (i + 1) % 10 == 0 or i + 1 == len(classes):
            elapsed = time.time() - t0
            print(f"  [{i+1}/169] {cls}: {eq:.4f}  (elapsed {elapsed:.0f}s)", flush=True)

    total_combos = sum(r["combos"] for r in results.values())
    assert total_combos == 1326, total_combos

    mean = sum(r["equity"] * r["combos"] for r in results.values()) / total_combos
    var = sum(((r["equity"] - mean) ** 2) * r["combos"] for r in results.values()) / total_combos

    payload = {
        "n_trials": n_trials,
        "mean": round(mean, 4),
        "variance": round(var, 6),
        "classes": results,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nWrote {out_path}")
    print(f"mean={mean:.4f} variance={var:.6f}")


if __name__ == "__main__":
    main()
