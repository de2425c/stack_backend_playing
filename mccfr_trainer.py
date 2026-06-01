"""
External Sampling MCCFR Trainer using PokerKit

Outputs SQLite in the existing policy database format:
  info_state: "{player}|p{player}:b{bucket}:h,{actions}"
  action_probs: pickled dict {action_id: probability}
"""

import copy
import random
import pickle
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

from pokerkit_adapter import PokerKitAdapter, Action, GameConfig

# Try to import Cython-optimized functions
try:
    from mccfr_core import get_strategy_fast, sample_action, update_regrets, update_strategy_sum
    USE_CYTHON = True
except ImportError:
    USE_CYTHON = False


@dataclass
class TrainerConfig:
    num_players: int = 6
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 10000
    seed: int = 42


class MCCFRTrainer:
    """External Sampling MCCFR with PokerKit game engine."""

    def __init__(self, config: TrainerConfig = None, load_abstractions: bool = True):
        self.config = config or TrainerConfig()
        random.seed(self.config.seed)

        # Game adapter
        game_config = GameConfig(
            num_players=self.config.num_players,
            small_blind=self.config.small_blind,
            big_blind=self.config.big_blind,
            starting_stack=self.config.starting_stack,
        )
        self.adapter = PokerKitAdapter(config=game_config, load_abstractions=load_abstractions)

        # Regret and strategy tables
        # Key: info_state string
        # Value: dict {action_id: cumulative_value}
        if USE_CYTHON:
            # Use regular dicts for Cython compatibility
            self.regret_sum: Dict[str, Dict[int, float]] = {}
            self.strategy_sum: Dict[str, Dict[int, float]] = {}
        else:
            self.regret_sum: Dict[str, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
            self.strategy_sum: Dict[str, Dict[int, float]] = defaultdict(lambda: defaultdict(float))

        # Stats
        self.iterations = 0

    def get_strategy(self, info_state: str, legal_actions: List[Action]) -> Dict[int, float]:
        """Get current strategy via regret matching."""
        if USE_CYTHON:
            regrets = dict(self.regret_sum.get(info_state, {}))
            action_ids = [int(a) for a in legal_actions]
            return get_strategy_fast(regrets, action_ids)

        regrets = self.regret_sum[info_state]

        # Regret matching: probability proportional to positive regret
        strategy = {}
        positive_sum = 0.0

        for action in legal_actions:
            r = max(0.0, regrets[int(action)])
            strategy[int(action)] = r
            positive_sum += r

        if positive_sum > 0:
            for action in legal_actions:
                strategy[int(action)] /= positive_sum
        else:
            # Uniform if no positive regrets
            uniform = 1.0 / len(legal_actions)
            for action in legal_actions:
                strategy[int(action)] = uniform

        return strategy

    def external_sampling(self, adapter: PokerKitAdapter, traverser: int) -> float:
        """
        External sampling MCCFR traversal.

        - At traverser's nodes: explore all actions, update regrets
        - At opponent's nodes: sample one action according to strategy
        - At terminal: return payoff

        Args:
            adapter: Game state adapter
            traverser: Player being updated this iteration

        Returns:
            Expected value for traverser
        """
        # Terminal check
        if adapter.is_terminal():
            return adapter.get_payoffs()[traverser]

        current_player = adapter.current_player
        if current_player is None:
            return adapter.get_payoffs()[traverser]

        legal_actions = adapter.get_legal_actions()
        if not legal_actions:
            return adapter.get_payoffs()[traverser]

        info_state = adapter.get_info_state(current_player)
        strategy = self.get_strategy(info_state, legal_actions)

        if current_player == traverser:
            # Traverser: explore ALL actions
            action_values = {}

            for action in legal_actions:
                # Deep copy game state
                child = self._copy_adapter(adapter)
                child.apply_action(action)
                action_values[int(action)] = self.external_sampling(child, traverser)

            # Expected value under current strategy
            ev = sum(strategy[int(a)] * action_values[int(a)] for a in legal_actions)

            # Update regrets and accumulate strategy
            action_ids = [int(a) for a in legal_actions]
            if USE_CYTHON:
                update_regrets(self.regret_sum, info_state, action_ids, action_values, strategy)
                update_strategy_sum(self.strategy_sum, info_state, action_ids, strategy)
            else:
                for action in legal_actions:
                    regret = action_values[int(action)] - ev
                    self.regret_sum[info_state][int(action)] += regret
                for action in legal_actions:
                    self.strategy_sum[info_state][int(action)] += strategy[int(action)]

            return ev

        else:
            # Opponent: sample ONE action
            actions = [int(a) for a in legal_actions]
            weights = [strategy[a] for a in actions]
            if USE_CYTHON:
                action_id = sample_action(actions, weights)
            else:
                action_id = random.choices(actions, weights=weights)[0]

            adapter.apply_action(Action(action_id))
            return self.external_sampling(adapter, traverser)

    def _copy_adapter(self, adapter: PokerKitAdapter) -> PokerKitAdapter:
        """Deep copy adapter for branching."""
        new_adapter = PokerKitAdapter.__new__(PokerKitAdapter)
        new_adapter.config = adapter.config
        new_adapter.game = copy.deepcopy(adapter.game)
        new_adapter.action_history = adapter.action_history.copy()
        new_adapter.street_history = [h.copy() for h in adapter.street_history]
        new_adapter.flop_abs = adapter.flop_abs  # Shared, read-only
        new_adapter.turn_abs = adapter.turn_abs
        return new_adapter

    def train(self, num_iterations: int, log_freq: int = 1000, checkpoint_freq: int = 0, checkpoint_path: str = None):
        """
        Run MCCFR training.

        Args:
            num_iterations: Number of iterations
            log_freq: Print progress every N iterations
            checkpoint_freq: Save checkpoint every N iterations (0 = never)
            checkpoint_path: Path for checkpoints
        """
        start_time = time.time()

        for i in range(num_iterations):
            # Start new hand
            self.adapter.new_game()

            # Update each player
            for player in range(self.config.num_players):
                adapter_copy = self._copy_adapter(self.adapter)
                self.external_sampling(adapter_copy, player)

            self.iterations += 1

            # Logging
            if self.iterations % log_freq == 0:
                elapsed = time.time() - start_time
                iters_per_sec = self.iterations / elapsed
                num_states = len(self.regret_sum)
                print(f"Iter {self.iterations:,} | {iters_per_sec:.1f} it/s | {num_states:,} states | {elapsed:.1f}s")

            # Checkpointing
            if checkpoint_freq > 0 and self.iterations % checkpoint_freq == 0 and checkpoint_path:
                self.save_checkpoint(f"{checkpoint_path}_iter{self.iterations}.pkl")

    def get_average_strategy(self) -> Dict[str, Dict[int, float]]:
        """Get average strategy (converges to Nash equilibrium)."""
        avg_strategy = {}

        for info_state, action_sums in self.strategy_sum.items():
            total = sum(action_sums.values())
            if total > 0:
                avg_strategy[info_state] = {a: s / total for a, s in action_sums.items()}
            else:
                # Uniform fallback
                num_actions = len(action_sums)
                if num_actions > 0:
                    avg_strategy[info_state] = {a: 1.0 / num_actions for a in action_sums}

        return avg_strategy

    def export_to_sqlite(self, db_path: str):
        """
        Export average strategy to SQLite in policy DB format.

        Format:
          info_state TEXT PRIMARY KEY
          action_probs BLOB (pickled dict)
        """
        avg_strategy = self.get_average_strategy()

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Create tables
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS policy (
                info_state TEXT PRIMARY KEY,
                action_probs BLOB
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Insert strategies
        for info_state, probs in avg_strategy.items():
            action_probs_blob = pickle.dumps(probs)
            cursor.execute(
                "INSERT OR REPLACE INTO policy (info_state, action_probs) VALUES (?, ?)",
                (info_state, action_probs_blob)
            )

        # Insert metadata
        cursor.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("iterations", str(self.iterations)))
        cursor.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("num_states", str(len(avg_strategy))))
        cursor.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S")))

        conn.commit()
        conn.close()
        print(f"Exported {len(avg_strategy):,} states to {db_path}")

    def save_checkpoint(self, path: str):
        """Save training state."""
        state = {
            "iterations": self.iterations,
            "regret_sum": dict(self.regret_sum),
            "strategy_sum": dict(self.strategy_sum),
        }
        with open(path, "wb") as f:
            pickle.dump(state, f)
        print(f"Checkpoint saved: {path}")

    def load_checkpoint(self, path: str):
        """Load training state."""
        with open(path, "rb") as f:
            state = pickle.load(f)
        self.iterations = state["iterations"]
        self.regret_sum = defaultdict(lambda: defaultdict(float), state["regret_sum"])
        self.strategy_sum = defaultdict(lambda: defaultdict(float), state["strategy_sum"])
        print(f"Loaded checkpoint: {self.iterations:,} iterations, {len(self.regret_sum):,} states")


# =============================================================================
# Quick test
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("MCCFR Trainer Test")
    print(f"Cython: {'ENABLED' if USE_CYTHON else 'DISABLED'}")
    print("=" * 60)

    # Test with small iteration count
    trainer = MCCFRTrainer(load_abstractions=False)
    print(f"\nStarting training (no abstractions for speed)...")

    trainer.train(num_iterations=100, log_freq=25)

    print(f"\nFinal: {trainer.iterations} iterations, {len(trainer.regret_sum)} info states")

    # Show sample strategies
    avg = trainer.get_average_strategy()
    print(f"\nSample strategies (first 5):")
    for i, (state, probs) in enumerate(list(avg.items())[:5]):
        print(f"  {state}: {probs}")

    # Test SQLite export
    print(f"\nExporting to test_policy.db...")
    trainer.export_to_sqlite("/tmp/test_policy.db")

    print("\nDone!")
