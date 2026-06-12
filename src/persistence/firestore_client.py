"""
Firestore client wrapper with in-memory fallback.

Uses Firebase Admin SDK when available, falls back to in-memory storage for testing.

All Firestore I/O is dispatched via ``asyncio.to_thread`` so the synchronous
Firebase Admin SDK does not block the asyncio event loop. Every other table
on the instance freezes if any wallet/hand/session/duel call runs inline
on the loop, so the wrappers are mandatory rather than cosmetic.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Optional
from datetime import datetime
from .models import HandLog, LedgerEntry, DuelRecord, DuelRating

logger = logging.getLogger(__name__)


class FirestoreClient:
    """
    Firestore wrapper with in-memory fallback for testing.

    Attempts to connect to Firestore using Firebase Admin SDK.
    Falls back to in-memory storage if Firebase is not configured.
    """

    def __init__(self, use_memory: bool = False):
        """
        Initialize Firestore client.

        Args:
            use_memory: Force in-memory mode (for testing)
        """
        self._db = None
        self._in_memory: dict[str, list] = {"hands": [], "ledger": []}
        # Wallet credits that failed to land (Firestore outage / txn error).
        # Drained by retry_pending_credits(), which app.py calls on a loop.
        # Each entry mirrors a doc in the `failed_credits` collection so a
        # process restart can resume delivery via load_failed_credits().
        self._pending_credits: list[dict] = []
        # Multiple server instances (8000 dev + 8001 prod) share one Firestore
        # project. open_sessions / failed_credits docs are stamped with the
        # owning instance's port so one instance's startup sweep can't refund
        # (or re-deliver) sessions that are alive and well on the other.
        self._instance_tag = os.environ.get("PORT", "8000")

        if not use_memory:
            self._try_init_firestore()

    def _try_init_firestore(self) -> None:
        """Attempt to initialize Firebase Admin SDK."""
        try:
            import firebase_admin
            from firebase_admin import firestore

            # Initialize Firebase if not already done
            # Expects GOOGLE_APPLICATION_CREDENTIALS env var
            if not firebase_admin._apps:
                logger.info("Initializing Firebase Admin SDK...")
                firebase_admin.initialize_app()

            self._db = firestore.client()
            logger.info("Firestore client initialized successfully")
        except Exception as e:
            # Fall back to in-memory mode
            logger.warning(f"Firestore initialization failed, using in-memory storage: {e}")

    @property
    def is_connected(self) -> bool:
        """Check if connected to Firestore."""
        return self._db is not None

    async def write_hand_log(self, hand_log: HandLog) -> None:
        """
        Write hand log to storage.

        Args:
            hand_log: Complete hand history to persist
        """
        data = hand_log.to_dict()

        if self._db:
            logger.info(f"Writing hand {hand_log.hand_id} to Firestore")
            await asyncio.to_thread(
                self._db.collection("hands").document(hand_log.hand_id).set, data
            )
            logger.info(f"Hand {hand_log.hand_id} written successfully")
        else:
            logger.warning(f"Writing hand {hand_log.hand_id} to in-memory (Firestore not connected)")
            self._in_memory["hands"].append(data)

    async def write_ledger_entries(self, entries: list[LedgerEntry]) -> None:
        """
        Write ledger entries to storage.

        Args:
            entries: List of chip movement records
        """
        if not entries:
            return

        if self._db:
            logger.info(f"Writing {len(entries)} ledger entries to Firestore")

            # Single thread hop per entry would N-multiply the cost.
            # One batched commit is one RTT no matter how many entries.
            def _batch_write():
                batch = self._db.batch()
                for entry in entries:
                    ref = self._db.collection("ledger").document(entry.entry_id)
                    batch.set(ref, entry.to_dict())
                batch.commit()

            await asyncio.to_thread(_batch_write)
            logger.info(f"Ledger entries written successfully")
        else:
            logger.warning(f"Writing {len(entries)} ledger entries to in-memory (Firestore not connected)")
            for entry in entries:
                data = entry.to_dict()
                self._in_memory["ledger"].append(data)

    def get_hand_log(self, hand_id: str) -> Optional[dict]:
        """
        Retrieve hand log by ID (SYNC — for use from non-async contexts).

        Prefer :meth:`get_hand` from async code so the Firestore call lands
        on the thread pool and the asyncio event loop keeps spinning.

        Args:
            hand_id: Unique hand identifier

        Returns:
            Hand log data or None if not found
        """
        if self._db:
            doc = self._db.collection("hands").document(hand_id).get()
            return doc.to_dict() if doc.exists else None
        else:
            for h in self._in_memory["hands"]:
                if h["hand_id"] == hand_id:
                    return h
            return None

    def get_ledger_entries(self, user_id: str, hand_id: Optional[str] = None) -> list[dict]:
        """
        Retrieve ledger entries for a user (SYNC — for debug endpoints).

        Args:
            user_id: User to look up
            hand_id: Optional filter by hand

        Returns:
            List of ledger entry dicts
        """
        if self._db:
            query = self._db.collection("ledger").where("user_id", "==", user_id)
            if hand_id:
                query = query.where("hand_id", "==", hand_id)
            return [doc.to_dict() for doc in query.stream()]
        else:
            entries = []
            for e in self._in_memory["ledger"]:
                if e["user_id"] == user_id:
                    if hand_id is None or e["hand_id"] == hand_id:
                        entries.append(e)
            return entries

    def get_all_hand_logs(self) -> list[dict]:
        """
        Retrieve all hand logs (SYNC — for debug endpoints only).

        Returns:
            List of all hand log dicts
        """
        if self._db:
            return [doc.to_dict() for doc in self._db.collection("hands").stream()]
        else:
            return list(self._in_memory["hands"])

    def get_all_ledger_entries(self) -> list[dict]:
        """
        Retrieve all ledger entries (SYNC — for debug endpoints only).

        Returns:
            List of all ledger entry dicts
        """
        if self._db:
            return [doc.to_dict() for doc in self._db.collection("ledger").stream()]
        else:
            return list(self._in_memory["ledger"])

    def clear(self) -> None:
        """Clear in-memory storage (for testing)."""
        self._in_memory = {"hands": [], "ledger": []}

    # =========================================================================
    # WALLET OPERATIONS
    # =========================================================================

    async def get_user_balance(self, user_id: str) -> int:
        """
        Get user balance in cents.

        Args:
            user_id: User to look up

        Returns:
            Balance in cents (0 if wallet doesn't exist)

        Note:
            Firestore stores balance as 'dollars' (whole numbers).
            We convert to cents: dollars * 100 = cents
        """
        if self._db:
            doc = await asyncio.to_thread(
                self._db.collection("wallets").document(user_id).get
            )
            if doc.exists:
                data = doc.to_dict()
                dollars = data.get("dollars", 0)
                return dollars * 100
            return 0
        else:
            # In-memory mode
            wallets = self._in_memory.get("wallets", {})
            dollars = wallets.get(user_id, 0)
            return dollars * 100

    async def deduct_balance(self, user_id: str, cents: int) -> int:
        """
        Deduct cents from user balance atomically.

        Args:
            user_id: User to deduct from
            cents: Amount to deduct in cents

        Returns:
            New balance in cents after deduction

        Raises:
            ValueError: If insufficient funds
        """
        if cents <= 0:
            raise ValueError("Deduction amount must be positive")

        if self._db:
            from google.cloud.firestore import SERVER_TIMESTAMP, transactional

            def _deduct_blocking() -> int:
                transaction = self._db.transaction()
                wallet_ref = self._db.collection("wallets").document(user_id)

                @transactional
                def deduct_in_transaction(txn, ref):
                    doc = ref.get(transaction=txn)
                    if not doc.exists:
                        raise ValueError(f"Wallet not found for user {user_id}")

                    data = doc.to_dict()
                    current_dollars = data.get("dollars", 0)
                    current_cents = current_dollars * 100

                    if current_cents < cents:
                        raise ValueError(
                            f"Insufficient balance: {current_cents} cents < {cents} cents"
                        )

                    new_cents = current_cents - cents
                    new_dollars = new_cents // 100
                    # Keep `updatedAt` in sync with the iOS DollarsService
                    # writes — the bankroll leaderboard uses it to drop
                    # dormant wallets, so table buy-ins must count as
                    # wallet activity.
                    txn.update(ref, {"dollars": new_dollars, "updatedAt": SERVER_TIMESTAMP})
                    return new_cents

                return deduct_in_transaction(transaction, wallet_ref)

            new_balance = await asyncio.to_thread(_deduct_blocking)
            logger.info(f"Deducted {cents} cents from {user_id}, new balance: {new_balance}")
            return new_balance
        else:
            # In-memory mode
            if "wallets" not in self._in_memory:
                self._in_memory["wallets"] = {}

            wallets = self._in_memory["wallets"]
            current_dollars = wallets.get(user_id, 0)
            current_cents = current_dollars * 100

            if current_cents < cents:
                raise ValueError(
                    f"Insufficient balance: {current_cents} cents < {cents} cents"
                )

            new_cents = current_cents - cents
            new_dollars = new_cents // 100
            wallets[user_id] = new_dollars
            logger.info(f"[In-Memory] Deducted {cents} cents from {user_id}, new balance: {new_cents}")
            return new_cents

    async def add_balance(self, user_id: str, cents: int) -> int:
        """
        Add cents to user balance atomically.

        Args:
            user_id: User to credit
            cents: Amount to add in cents

        Returns:
            New balance in cents after addition
        """
        if cents <= 0:
            raise ValueError("Addition amount must be positive")

        if self._db:
            from google.cloud.firestore import SERVER_TIMESTAMP, transactional

            def _add_blocking() -> int:
                transaction = self._db.transaction()
                wallet_ref = self._db.collection("wallets").document(user_id)

                @transactional
                def add_in_transaction(txn, ref):
                    doc = ref.get(transaction=txn)
                    if doc.exists:
                        data = doc.to_dict()
                        current_dollars = data.get("dollars", 0)
                    else:
                        current_dollars = 0

                    current_cents = current_dollars * 100
                    new_cents = current_cents + cents
                    new_dollars = new_cents // 100

                    # `updatedAt` mirrors the iOS DollarsService writes; the
                    # bankroll leaderboard treats it as wallet activity.
                    if doc.exists:
                        txn.update(ref, {"dollars": new_dollars, "updatedAt": SERVER_TIMESTAMP})
                    else:
                        txn.set(ref, {"dollars": new_dollars, "updatedAt": SERVER_TIMESTAMP})

                    return new_cents

                return add_in_transaction(transaction, wallet_ref)

            new_balance = await asyncio.to_thread(_add_blocking)
            logger.info(f"Added {cents} cents to {user_id}, new balance: {new_balance}")
            return new_balance
        else:
            # In-memory mode
            if "wallets" not in self._in_memory:
                self._in_memory["wallets"] = {}

            wallets = self._in_memory["wallets"]
            current_dollars = wallets.get(user_id, 0)
            current_cents = current_dollars * 100
            new_cents = current_cents + cents
            new_dollars = new_cents // 100
            wallets[user_id] = new_dollars
            logger.info(f"[In-Memory] Added {cents} cents to {user_id}, new balance: {new_cents}")
            return new_cents

    # =========================================================================
    # SESSION OPERATIONS
    # =========================================================================

    async def get_hand(self, hand_id: str) -> Optional[dict]:
        """
        Async wrapper for get_hand_log — dispatches the blocking Firestore
        ``.get()`` onto the default thread pool so the asyncio loop stays
        free for other coroutines.

        Args:
            hand_id: Unique hand identifier

        Returns:
            Hand log data or None if not found
        """
        if self._db:
            doc = await asyncio.to_thread(
                self._db.collection("hands").document(hand_id).get
            )
            return doc.to_dict() if doc.exists else None
        # In-memory fallback — no I/O, run inline.
        return self.get_hand_log(hand_id)

    async def get_hands(self, hand_ids: list[str]) -> list[Optional[dict]]:
        """
        Parallel batch read for many hands.

        Each fetch hops onto the thread pool, so N reads share roughly one
        Firestore RTT instead of being serialised on the event loop. Used by
        :func:`session.processor._fetch_hands` to keep post-session analysis
        from stalling the server.

        Args:
            hand_ids: List of hand IDs to fetch

        Returns:
            List of dicts (or None for hands that don't exist), in the same
            order as ``hand_ids``.
        """
        if not hand_ids:
            return []
        if self._db:
            tasks = [self.get_hand(hid) for hid in hand_ids]
            return await asyncio.gather(*tasks)
        return [self.get_hand_log(hid) for hid in hand_ids]

    async def write_session(self, session_id: str, session_data: dict) -> None:
        """
        Write session record to storage.

        Args:
            session_id: Unique session identifier
            session_data: Complete session data including analysis
        """
        if self._db:
            logger.info(f"Writing session {session_id} to Firestore")
            await asyncio.to_thread(
                self._db.collection("sessions").document(session_id).set,
                session_data,
            )
            logger.info(f"Session {session_id} written successfully")
        else:
            logger.warning(f"Writing session {session_id} to in-memory (Firestore not connected)")
            if "sessions" not in self._in_memory:
                self._in_memory["sessions"] = []
            self._in_memory["sessions"].append(session_data)

    async def merge_bot_session_analysis(self, client_session_id: str, partial_data: dict) -> None:
        """
        Merge analysis fields into the iOS-owned bot_sessions/{client_session_id} doc.

        The iOS client creates the doc on session start with metadata
        (start_time, hand_ids, profit_cents, etc.). The backend only writes its
        own derived fields (e.g. the `analysis` map) here, so iOS metadata is
        never overwritten.
        """
        if not client_session_id:
            logger.warning("merge_bot_session_analysis called with empty client_session_id; skipping")
            return

        if self._db:
            logger.info(f"Merging analysis into bot_sessions/{client_session_id}")

            def _do_merge():
                self._db.collection("bot_sessions").document(client_session_id).set(
                    partial_data, merge=True
                )

            await asyncio.to_thread(_do_merge)
            logger.info(f"bot_sessions/{client_session_id} merged successfully")
        else:
            logger.warning(
                f"Merging bot_session {client_session_id} to in-memory (Firestore not connected)"
            )
            if "bot_sessions" not in self._in_memory:
                self._in_memory["bot_sessions"] = {}
            existing = self._in_memory["bot_sessions"].get(client_session_id, {})
            existing.update(partial_data)
            self._in_memory["bot_sessions"][client_session_id] = existing

    async def get_session(self, session_id: str) -> Optional[dict]:
        """
        Retrieve session by ID.

        Args:
            session_id: Unique session identifier

        Returns:
            Session data or None if not found
        """
        if self._db:
            doc = await asyncio.to_thread(
                self._db.collection("sessions").document(session_id).get
            )
            return doc.to_dict() if doc.exists else None
        else:
            for s in self._in_memory.get("sessions", []):
                if s.get("session_id") == session_id:
                    return s
            return None

    async def get_user_sessions(self, user_id: str, limit: int = 20) -> list[dict]:
        """
        Retrieve recent sessions for a user.

        Args:
            user_id: User to look up
            limit: Maximum number of sessions to return

        Returns:
            List of session dicts, most recent first
        """
        if self._db:
            # Simple query without ordering (avoids composite index requirement)
            def _blocking_query():
                query = self._db.collection("sessions").where("user_id", "==", user_id)
                return [doc.to_dict() for doc in query.stream()]

            sessions = await asyncio.to_thread(_blocking_query)
            # Sort in Python by ended_at descending
            sessions.sort(key=lambda x: x.get("ended_at", ""), reverse=True)
            return sessions[:limit]
        else:
            sessions = [
                s for s in self._in_memory.get("sessions", [])
                if s.get("user_id") == user_id
            ]
            # Sort by ended_at descending
            sessions.sort(key=lambda x: x.get("ended_at", ""), reverse=True)
            return sessions[:limit]

    # =========================================================================
    # DUEL OPERATIONS
    # =========================================================================

    async def write_duel_record(self, duel_record: DuelRecord) -> None:
        """
        Write duel match record to storage.

        Args:
            duel_record: Complete duel match result
        """
        data = duel_record.to_dict()

        if self._db:
            logger.info(f"Writing duel {duel_record.match_id} to Firestore")
            await asyncio.to_thread(
                self._db.collection("duels").document(duel_record.match_id).set, data
            )
            logger.info(f"Duel {duel_record.match_id} written successfully")
        else:
            logger.warning(f"Writing duel {duel_record.match_id} to in-memory (Firestore not connected)")
            if "duels" not in self._in_memory:
                self._in_memory["duels"] = []
            self._in_memory["duels"].append(data)

    async def get_user_duels(self, user_id: str, limit: int = 20) -> list[dict]:
        """
        Retrieve recent duel records for a user.

        Args:
            user_id: User to look up
            limit: Maximum number of duels to return

        Returns:
            List of duel dicts, most recent first
        """
        if self._db:
            def _blocking_query():
                query = self._db.collection("duels").where(
                    "participant_ids", "array_contains", user_id
                )
                return [doc.to_dict() for doc in query.stream()]

            duels = await asyncio.to_thread(_blocking_query)
            duels.sort(key=lambda x: x.get("ended_at", ""), reverse=True)
            return duels[:limit]
        else:
            duels = [
                d for d in self._in_memory.get("duels", [])
                if user_id in d.get("participant_ids", [])
            ]
            duels.sort(key=lambda x: x.get("ended_at", ""), reverse=True)
            return duels[:limit]

    # =========================================================================
    # DUEL RATING OPERATIONS
    # =========================================================================

    async def get_duel_rating(self, user_id: str) -> Optional[dict]:
        """
        Get player's duel rating, or None if no rating exists.

        Args:
            user_id: User to look up

        Returns:
            Rating dict or None if not found
        """
        if self._db:
            doc = await asyncio.to_thread(
                self._db.collection("duel_ratings").document(user_id).get
            )
            return doc.to_dict() if doc.exists else None
        else:
            return self._in_memory.get("duel_ratings", {}).get(user_id)

    async def get_duel_ratings(self, user_ids: list[str]) -> dict[str, Optional[dict]]:
        """
        Parallel batch read for many duel ratings.

        Used by the bot persona pool so picking an opponent doesn't serialise
        70 Firestore reads on the event loop.

        Args:
            user_ids: List of user IDs to look up

        Returns:
            Mapping user_id → rating dict (or None when no rating exists).
        """
        if not user_ids:
            return {}
        if self._db:
            results = await asyncio.gather(*(self.get_duel_rating(uid) for uid in user_ids))
            return dict(zip(user_ids, results))
        return {uid: self._in_memory.get("duel_ratings", {}).get(uid) for uid in user_ids}

    async def update_duel_rating(
        self,
        user_id: str,
        rating: float,
        rd: float,
        wins: int,
        losses: int,
    ) -> None:
        """
        Update player's duel rating after a match.

        Args:
            user_id: User to update
            rating: New Glicko rating
            rd: New rating deviation
            wins: Total wins
            losses: Total losses
        """
        data = {
            "user_id": user_id,
            "rating": rating,
            "rd": rd,
            "wins": wins,
            "losses": losses,
            "last_played": datetime.utcnow().isoformat(),
        }
        if self._db:
            logger.info(f"Updating duel rating for {user_id}: rating={rating:.0f}, rd={rd:.0f}")
            await asyncio.to_thread(
                self._db.collection("duel_ratings").document(user_id).set, data
            )
        else:
            if "duel_ratings" not in self._in_memory:
                self._in_memory["duel_ratings"] = {}
            self._in_memory["duel_ratings"][user_id] = data
            logger.info(f"[In-Memory] Updated duel rating for {user_id}: rating={rating:.0f}, rd={rd:.0f}")

    # =========================================================================
    # PENDING NOTICES (user-facing "you were disconnected, chips refunded")
    # =========================================================================

    async def write_pending_notice(self, user_id: str, notice: dict) -> None:
        """
        Queue a one-time notice for the iOS client to show on next launch.

        Written to users/{user_id}/pending_notices/{auto-id}. The client
        reads, displays, and deletes the docs. Best-effort: callers should
        not let a notice failure break a refund path.
        """
        data = dict(notice)
        data.setdefault("user_id", user_id)
        data.setdefault("created_at", datetime.utcnow().isoformat())

        if self._db:
            doc_id = uuid.uuid4().hex
            await asyncio.to_thread(
                self._db.collection("users")
                .document(user_id)
                .collection("pending_notices")
                .document(doc_id)
                .set,
                data,
            )
            logger.info(f"Pending notice written for {user_id}: {data.get('type')}")
        else:
            self._in_memory.setdefault("pending_notices", {}).setdefault(user_id, []).append(data)

    # =========================================================================
    # OPEN SESSION LEDGER (crash-safe record of wallet money sitting on tables)
    # =========================================================================
    #
    # One doc per user at open_sessions/{user_id}, created when their wallet
    # is debited for a seat (cash buy-in) or a duel entry fee, incremented on
    # rebuy/top-up, and deleted when the server consciously settles the
    # session (leave, grace-expiry refund, duel completion/refund). Any doc
    # still present at startup belongs to a session the process lost track of
    # (crash / unclean restart) — the sweep refunds total_in_cents.

    async def upsert_open_session(self, user_id: str, data: dict) -> None:
        """Create/replace the user's open-session ledger doc."""
        doc = dict(data)
        doc.setdefault("user_id", user_id)
        doc.setdefault("status", "open")
        doc.setdefault("server_port", self._instance_tag)
        doc.setdefault("created_at", datetime.utcnow().isoformat())

        if self._db:
            await asyncio.to_thread(
                self._db.collection("open_sessions").document(user_id).set, doc
            )
        else:
            self._in_memory.setdefault("open_sessions", {})[user_id] = doc

    async def increment_open_session(self, user_id: str, cents: int) -> None:
        """Add cents to the user's open-session total (rebuy / top-up)."""
        if cents <= 0:
            return
        if self._db:
            from google.cloud import firestore as gc_firestore

            await asyncio.to_thread(
                self._db.collection("open_sessions").document(user_id).update,
                {
                    "total_in_cents": gc_firestore.Increment(cents),
                    "updated_at": datetime.utcnow().isoformat(),
                },
            )
        else:
            doc = self._in_memory.setdefault("open_sessions", {}).get(user_id)
            if doc is not None:
                doc["total_in_cents"] = doc.get("total_in_cents", 0) + cents

    async def delete_open_session(self, user_id: str) -> None:
        """Remove the user's open-session ledger doc (session settled)."""
        if self._db:
            await asyncio.to_thread(
                self._db.collection("open_sessions").document(user_id).delete
            )
        else:
            self._in_memory.setdefault("open_sessions", {}).pop(user_id, None)

    async def list_open_sessions(self) -> list[dict]:
        """This instance's open-session docs — used by the startup orphan sweep.

        Filtered in Python rather than the query so docs written before the
        server_port stamp existed (no field) are still claimed by whichever
        instance sweeps them first instead of leaking forever.
        """
        if self._db:
            def _query():
                return [doc.to_dict() for doc in self._db.collection("open_sessions").stream()]

            docs = await asyncio.to_thread(_query)
        else:
            docs = list(self._in_memory.get("open_sessions", {}).values())
        return [d for d in docs if d.get("server_port", self._instance_tag) == self._instance_tag]

    # =========================================================================
    # RELIABLE WALLET CREDITS (refunds that must not be lost)
    # =========================================================================

    async def credit_balance_reliable(
        self,
        user_id: str,
        cents: int,
        reason: str,
        open_session_user: Optional[str] = None,
    ) -> bool:
        """
        Credit a wallet; on failure, enqueue for retry instead of losing it.

        Failed credits go to an in-memory queue (drained by
        retry_pending_credits) AND a `failed_credits` doc so a restart can
        resume delivery. Returns True if the credit landed immediately.

        Args:
            open_session_user: if set, the open_sessions/{uid} ledger doc is
                deleted once the credit eventually lands (the retry path owns
                the doc from this point, so the startup sweep can't also
                refund it).
        """
        if cents <= 0:
            return True
        try:
            await self.add_balance(user_id, cents)
            return True
        except Exception as e:
            entry = {
                "entry_id": f"credit_{user_id}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
                "user_id": user_id,
                "cents": cents,
                "reason": reason,
                "open_session_user": open_session_user,
                "server_port": self._instance_tag,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
            }
            self._pending_credits.append(entry)
            logger.critical(
                "[CREDIT][CRITICAL] FAILED to credit %d cents to %s (%s): %r — queued for retry",
                cents, user_id, reason, e,
            )
            # Best-effort persistence so the credit survives a process crash.
            try:
                if self._db:
                    await asyncio.to_thread(
                        self._db.collection("failed_credits").document(entry["entry_id"]).set,
                        entry,
                    )
            except Exception:
                pass
            return False

    def has_pending_credit(self, user_id: str) -> bool:
        """True if a queued (undelivered) wallet credit exists for this user."""
        return any(e.get("user_id") == user_id for e in self._pending_credits)

    async def retry_pending_credits(self) -> None:
        """Drain the failed-credit queue. Called periodically by app.py."""
        for entry in list(self._pending_credits):
            try:
                await self.add_balance(entry["user_id"], entry["cents"])
            except Exception:
                continue  # Still failing — keep queued.

            self._pending_credits.remove(entry)
            logger.warning(
                "[CREDIT][RETRY] Delivered %d cents to %s (%s)",
                entry["cents"], entry["user_id"], entry["reason"],
            )
            if entry.get("open_session_user"):
                try:
                    await self.delete_open_session(entry["open_session_user"])
                except Exception:
                    pass
            try:
                if self._db:
                    await asyncio.to_thread(
                        self._db.collection("failed_credits")
                        .document(entry["entry_id"])
                        .update,
                        {"status": "delivered", "delivered_at": datetime.utcnow().isoformat()},
                    )
            except Exception:
                pass

    async def load_failed_credits(self) -> int:
        """
        Load undelivered failed_credits docs from a previous run into the
        retry queue. Called once at startup. Returns the number loaded.
        """
        if not self._db:
            return 0

        def _query():
            return [
                doc.to_dict()
                for doc in self._db.collection("failed_credits")
                .where("status", "==", "pending")
                .stream()
            ]

        try:
            entries = await asyncio.to_thread(_query)
        except Exception as e:
            logger.warning(f"Could not load failed_credits at startup: {e}")
            return 0

        known = {e["entry_id"] for e in self._pending_credits}
        loaded = 0
        for entry in entries:
            # Only resume credits this instance queued — the other instance's
            # live process is already retrying its own (loading them here
            # would deliver them twice).
            if entry.get("server_port", self._instance_tag) != self._instance_tag:
                continue
            if entry.get("entry_id") not in known:
                self._pending_credits.append(entry)
                loaded += 1
        if loaded:
            logger.warning(f"Loaded {loaded} undelivered wallet credits from previous run")
        return loaded
