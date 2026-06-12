"""
TableManager - Orchestrates multiple poker tables.

Routes players to tables, tracks user->table mapping,
and forwards commands to the appropriate TableRunner.
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from ..engine import TableConfig
from ..models import PlayerIdentity, Chips, ClientAction, generate_table_id
from .runner import TableRunner

logger = logging.getLogger(__name__)
from .commands import (
    JoinTableCommand,
    LeaveTableCommand,
    PlayerActionCommand,
    StartHandCommand,
    GetSnapshotCommand,
    GetSnapshotsBatchCommand,
    GetActionRequestCommand,
)

if TYPE_CHECKING:
    from ..persistence import HandLogger, FirestoreClient


class TableManager:
    """
    Orchestrates multiple poker tables.

    Routes players to tables, tracks user->table mapping,
    and forwards commands to the appropriate TableRunner.
    """

    def __init__(
        self,
        hand_logger: Optional["HandLogger"] = None,
        firestore: Optional["FirestoreClient"] = None,
    ):
        self._tables: dict[str, TableRunner] = {}  # table_id -> runner
        self._user_tables: dict[str, str] = {}     # user_id -> table_id
        self._hand_logger = hand_logger
        self._firestore = firestore
        self._stake_configs: dict[str, TableConfig] = self._build_stake_configs()

    @staticmethod
    def _build_stake_configs() -> dict[str, TableConfig]:
        # Cash-game tiers: (suffix, small_blind_cents, big_blind_cents).
        # Buy-in range is 20bb-200bb; rebuy/top-up cap (100bb) is computed at runtime.
        cash_tiers = [
            ("0p5_1", 50, 100),
            ("1_2", 100, 200),
            ("2_5", 200, 500),
            ("5_10", 500, 1000),
            ("10_25", 1000, 2500),
            ("25_50", 2500, 5000),
        ]
        configs: dict[str, TableConfig] = {}
        for suffix, sb, bb in cash_tiers:
            min_buy = 20 * bb
            max_buy = 200 * bb
            six_max_id = f"nlh_{suffix}"
            hu_id = f"nlh_{suffix}_hu"
            configs[six_max_id] = TableConfig(
                stake_id=six_max_id,
                small_blind_cents=sb,
                big_blind_cents=bb,
                min_buy_in_cents=min_buy,
                max_buy_in_cents=max_buy,
                min_players_to_start=2,  # Allow 2-player games on 6-max
            )
            configs[hu_id] = TableConfig(
                stake_id=hu_id,
                small_blind_cents=sb,
                big_blind_cents=bb,
                min_buy_in_cents=min_buy,
                max_buy_in_cents=max_buy,
                max_players=2,
                min_players_to_start=2,
            )
        # Duel mode stakes - ALWAYS 5¢/10¢ blinds, entry fee is separate.
        # 50BB = 500 chips, 15BB = 150 chips. Fixed buy-in.
        configs["duel_50bb"] = TableConfig(
            stake_id="duel_50bb",
            small_blind_cents=5,
            big_blind_cents=10,
            min_buy_in_cents=500,
            max_buy_in_cents=500,
            max_players=2,
            min_players_to_start=2,
        )
        configs["duel_15bb"] = TableConfig(
            stake_id="duel_15bb",
            small_blind_cents=5,
            big_blind_cents=10,
            min_buy_in_cents=150,
            max_buy_in_cents=150,
            max_players=2,
            min_players_to_start=2,
        )
        return configs

    def create_table(self, stake_id: str) -> str:
        """Create a new table for a stake level."""
        config = self._stake_configs.get(stake_id)
        if config is None:
            raise ValueError(f"Unknown stake: {stake_id}")

        table_id = generate_table_id()

        runner = TableRunner(table_id, config, self._hand_logger)
        runner.start()
        self._tables[table_id] = runner

        return table_id

    async def add_player(
        self,
        user_id: str,
        stake_id: str,
        buy_in: Chips,
        player: PlayerIdentity,
        table_id: Optional[str] = None,
    ) -> tuple[str, int]:
        """
        Add a player to a table at the given stake level.

        Returns (table_id, seat).
        If table_id is provided, joins that specific table.
        Otherwise finds existing table with open seats or creates new one.

        Financial invariant: if we debit the wallet but fail to seat the
        player (table full, engine error, cancellation, …) we MUST refund
        the buy-in. The try/finally below guards every failure path. The
        refund is wrapped in `asyncio.shield` so that a cancelled outer
        task still gets the wallet credit applied — losing the user's
        money on a transient cancellation would be far worse than
        delaying the cancellation by one Firestore RTT.
        """
        if user_id in self._user_tables:
            raise ValueError("User already at a table")

        # Validate stake_id and buy-in range BEFORE any balance deduction so that
        # rejected joins don't accidentally charge the player's wallet.
        stake_config = self._stake_configs.get(stake_id)
        if stake_config is None:
            raise ValueError(f"Unknown stake: {stake_id}")
        if not (stake_config.min_buy_in_cents <= buy_in.amount <= stake_config.max_buy_in_cents):
            raise ValueError(
                f"BUY_IN_OUT_OF_RANGE: {buy_in.amount} not in "
                f"[{stake_config.min_buy_in_cents}, {stake_config.max_buy_in_cents}]"
            )

        debited_cents = 0
        seated = False
        # Tracks the table_id we created in this call (vs. one we found
        # already populated). If the join fails, we tear it down so the
        # runner task + empty queue don't linger.
        created_table_id: Optional[str] = None
        # Duel stacks are virtual chips at fixed 5¢/10¢ blinds — the real
        # money is the entry fee, debited at JOIN_DUEL time. Debiting the
        # buy-in here as well double-charged duel players (the table stack
        # was never credited back at match end).
        is_duel_stake = stake_id.startswith("duel_")
        is_real_user = (
            self._firestore is not None
            and not user_id.startswith(("bot_", "user_bot_"))
            and not is_duel_stake
        )

        try:
            # Check and deduct balance for non-bot players
            if is_real_user:
                balance = await self._firestore.get_user_balance(user_id)
                if balance < buy_in.amount:
                    raise ValueError(
                        f"INSUFFICIENT_BALANCE: {balance} cents < {buy_in.amount} cents"
                    )
                await self._firestore.deduct_balance(user_id, buy_in.amount)
                debited_cents = buy_in.amount

            if table_id:
                runner = self._tables.get(table_id)
                if runner is None:
                    raise ValueError(f"Table {table_id} not found")
            else:
                # Find or create table
                runner = self._find_table_with_seats(stake_id)
                if runner is None:
                    table_id = self.create_table(stake_id)
                    created_table_id = table_id
                    runner = self._tables[table_id]

            # Submit join command
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            await runner.submit(JoinTableCommand(
                user_id=user_id,
                player=player,
                buy_in=buy_in,
                result_future=future,
            ))

            seat, snapshot = await future
            self._user_tables[user_id] = runner.table_id
            seated = True

            # Crash-safe ledger: record the wallet money now sitting on the
            # table. If the process dies before the session settles, the
            # startup sweep refunds total_in_cents. Best-effort — a ledger
            # write failure must not fail the join.
            if debited_cents > 0:
                try:
                    await self._firestore.upsert_open_session(user_id, {
                        "mode": "cash",
                        "table_id": runner.table_id,
                        "stake_id": stake_id,
                        "total_in_cents": debited_cents,
                    })
                except Exception as ledger_err:
                    logger.warning(
                        "[ADD_PLAYER] Failed to write open-session ledger for %s: %r",
                        user_id, ledger_err,
                    )

            return (runner.table_id, seat)
        finally:
            # Tear down the table we created if the join failed, so the
            # runner task + empty queue don't linger forever as a zombie.
            if created_table_id is not None and not seated:
                runner_to_stop = self._tables.pop(created_table_id, None)
                if runner_to_stop is not None:
                    try:
                        await runner_to_stop.stop()
                    except Exception as stop_err:
                        logger.warning(
                            "[ADD_PLAYER] Error stopping zombie table %s: %r",
                            created_table_id, stop_err,
                        )
                    else:
                        logger.info(
                            "[ADD_PLAYER] Stopped zombie table %s after join failure",
                            created_table_id,
                        )

            if debited_cents > 0 and not seated:
                # Refund via the reliable-credit path. Shield from outer-task
                # cancellation so the wallet credit always lands; if Firestore
                # is down the credit is queued (in memory + failed_credits doc)
                # and retried until delivered.
                try:
                    await asyncio.shield(
                        self._firestore.credit_balance_reliable(
                            user_id, debited_cents, "seat_acquisition_failed"
                        )
                    )
                    logger.warning(
                        "[ADD_PLAYER] Refunded %d cents to %s after seat-acquisition failure",
                        debited_cents, user_id,
                    )
                except asyncio.CancelledError:
                    # Outer task was cancelled. asyncio.shield kept the inner
                    # credit task running; the refund WILL complete (or queue).
                    # Re-raise so the caller still sees the cancellation.
                    logger.warning(
                        "[ADD_PLAYER] Outer task cancelled during refund of %d cents to %s; "
                        "shielded refund task continues in background",
                        debited_cents, user_id,
                    )
                    raise

    async def remove_player(self, user_id: str) -> Chips:
        """Remove a player from their table. Returns final chips."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(LeaveTableCommand(
            user_id=user_id,
            result_future=future,
        ))

        chips = await future
        del self._user_tables[user_id]

        # Clean up table if no human players remain
        if not runner.has_human_players():
            print(f"[TABLE] Cleaning up empty table {table_id}")
            await runner.stop()
            del self._tables[table_id]
            # Also remove any remaining bots from user_tables
            bot_users = [uid for uid, tid in self._user_tables.items() if tid == table_id]
            for bot_id in bot_users:
                del self._user_tables[bot_id]

        return chips

    async def route_action(
        self,
        user_id: str,
        hand_id: str,
        action: ClientAction,
        amount: Optional[Chips] = None,
        decision_metadata: Optional[dict] = None,
    ) -> list:
        """Route a player action to their table. Returns events."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(PlayerActionCommand(
            user_id=user_id,
            hand_id=hand_id,
            action=action,
            amount=amount,
            result_future=future,
            decision_metadata=decision_metadata,
        ))

        return await future

    async def start_hand(self, table_id: str) -> list:
        """Start a new hand at a table. Returns events."""
        runner = self._tables.get(table_id)
        if runner is None:
            raise ValueError("Table not found")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(StartHandCommand(result_future=future))

        return await future

    def get_table_for_user(self, user_id: str) -> Optional[str]:
        """Get the table ID for a user, or None if not seated."""
        return self._user_tables.get(user_id)

    def get_player_identity(self, user_id: str) -> Optional[tuple[int, str]]:
        """Return (seat_index, display_name) for a seated user, or None."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            return None
        runner = self._tables.get(table_id)
        if not runner:
            return None
        for idx, seat in enumerate(runner._engine._seats):
            if seat and seat.player and seat.player.user_id == user_id:
                return idx, seat.player.display_name
        return None

    async def get_snapshot(self, user_id: str):
        """Get table snapshot for a user."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(GetSnapshotCommand(
            user_id=user_id,
            result_future=future,
        ))

        return await future

    async def get_snapshots_batch(self, table_id: str, user_ids: list[str]) -> dict:
        """Get snapshots for many users in a single runner round-trip.

        Returns dict[user_id -> TableSnapshotMessage]. Users not seated at
        the table are silently omitted from the result (race with leave).
        Empty input yields an empty dict.
        """
        if not user_ids:
            return {}
        runner = self._tables.get(table_id)
        if runner is None:
            raise ValueError("Table not found")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(GetSnapshotsBatchCommand(
            user_ids=list(user_ids),
            result_future=future,
        ))
        return await future

    async def get_action_request(self, user_id: str):
        """Get action request for a user who needs to act."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(GetActionRequestCommand(
            user_id=user_id,
            result_future=future,
        ))

        return await future

    def _find_table_with_seats(self, stake_id: str) -> Optional[TableRunner]:
        """Find an existing table with open seats for the stake level."""
        for runner in self._tables.values():
            if runner._config.stake_id == stake_id and runner.has_open_seats():
                return runner
        return None

    async def try_rebuy(self, user_id: str, table_id: str, seat: int) -> Optional[tuple[int, int]]:
        """
        Attempt auto-rebuy for a bust player.

        Tops up TO 100bb of the table's big blind, not adds 100bb.
        Returns (rebuy_amount, new_stack) or None if rebuy failed.

        Financial invariant: if `deduct_balance` succeeds but the engine
        seat update fails (cancellation, KeyError, …) the user has paid
        but their stack didn't change. The try/finally below refunds
        best-effort via `asyncio.shield`, mirroring the P0-3 pattern
        in `add_player`.
        """
        # Skip bots - they don't need real chip balance
        if not self._firestore or user_id.startswith(("bot_", "user_bot_")):
            return None

        runner = self._tables.get(table_id)
        if not runner:
            return None

        seat_state = runner._engine._seats[seat]
        if not seat_state:
            return None

        # Cap rebuy at 100bb of this table's big blind.
        max_stack = 100 * runner._config.big_blind_cents
        current_chips = seat_state.chips
        if current_chips >= max_stack:
            return None  # Already at or above max

        rebuy_amount = max_stack - current_chips

        debited_cents = 0
        applied = False
        try:
            balance = await self._firestore.get_user_balance(user_id)
            if balance < rebuy_amount:
                return None

            await self._firestore.deduct_balance(user_id, rebuy_amount)
            debited_cents = rebuy_amount

            seat_state.chips = max_stack
            applied = True
            print(f"[REBUY] Topped up {user_id} by {rebuy_amount} cents to {max_stack}")

            # Keep the crash-safe ledger in step with the wallet debit.
            try:
                await self._firestore.increment_open_session(user_id, rebuy_amount)
            except Exception as ledger_err:
                logger.warning(
                    "[REBUY] Failed to increment open-session ledger for %s: %r",
                    user_id, ledger_err,
                )

            return (rebuy_amount, max_stack)
        except Exception as e:
            print(f"[REBUY] Failed for {user_id}: {e}")
            return None
        finally:
            if debited_cents > 0 and not applied:
                try:
                    await asyncio.shield(
                        self._firestore.credit_balance_reliable(
                            user_id, debited_cents, "rebuy_apply_failed"
                        )
                    )
                    logger.warning(
                        "[REBUY] Refunded %d cents to %s after rebuy apply failure",
                        debited_cents, user_id,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "[REBUY] Outer task cancelled during refund of %d cents to %s; "
                        "shielded refund task continues in background",
                        debited_cents, user_id,
                    )
                    raise

    async def request_topup(self, user_id: str) -> tuple[int, int]:
        """
        Request a manual top-up (queued for next hand start).

        Returns (topup_amount, projected_new_stack).
        Raises ValueError if user not at table, already at max, or insufficient balance.

        Financial invariant: if `deduct_balance` succeeds but the engine
        `pending_topup` update fails (cancellation between the await and
        the assignment), the user has paid but their queued top-up is
        lost. The try/finally below refunds best-effort via
        `asyncio.shield`, mirroring the P0-3 pattern in `add_player`.
        """
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables.get(table_id)
        if not runner:
            raise ValueError("Table not found")

        # Find user's seat
        seat_state = None
        for seat in runner._engine._seats:
            if seat and seat.player and seat.player.user_id == user_id:
                seat_state = seat
                break

        if seat_state is None:
            raise ValueError("User not seated at table")

        # Calculate top-up amount: bring stack + pending back to 100bb of this table's big blind.
        max_stack = 100 * runner._config.big_blind_cents
        current_effective = seat_state.chips + seat_state.pending_topup
        if current_effective >= max_stack:
            raise ValueError("Already at maximum stack")

        topup_amount = max_stack - current_effective

        # Check and deduct wallet balance. Track for refund-on-fail.
        debited_cents = 0
        applied = False
        try:
            if self._firestore and not user_id.startswith(("bot_", "user_bot_")):
                balance = await self._firestore.get_user_balance(user_id)
                if balance < topup_amount:
                    raise ValueError(f"INSUFFICIENT_BALANCE: {balance} cents < {topup_amount} cents")
                await self._firestore.deduct_balance(user_id, topup_amount)
                debited_cents = topup_amount

            # Queue the pending top-up (applied at next hand start)
            seat_state.pending_topup += topup_amount
            new_stack = seat_state.chips + seat_state.pending_topup
            applied = True

            print(f"[TOPUP] Queued {topup_amount} cents for {user_id}, new projected stack: {new_stack}")

            # Keep the crash-safe ledger in step with the wallet debit.
            if debited_cents > 0:
                try:
                    await self._firestore.increment_open_session(user_id, debited_cents)
                except Exception as ledger_err:
                    logger.warning(
                        "[TOPUP] Failed to increment open-session ledger for %s: %r",
                        user_id, ledger_err,
                    )

            return (topup_amount, new_stack)
        finally:
            if debited_cents > 0 and not applied:
                try:
                    await asyncio.shield(
                        self._firestore.credit_balance_reliable(
                            user_id, debited_cents, "topup_queue_failed"
                        )
                    )
                    logger.warning(
                        "[TOPUP] Refunded %d cents to %s after queue failure",
                        debited_cents, user_id,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "[TOPUP] Outer task cancelled during refund of %d cents to %s; "
                        "shielded refund task continues in background",
                        debited_cents, user_id,
                    )
                    raise

    async def shutdown(self) -> None:
        """Stop all table runners."""
        for runner in self._tables.values():
            await runner.stop()
