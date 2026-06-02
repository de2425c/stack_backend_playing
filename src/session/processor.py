"""
Session processor - analyzes completed sessions.

This is the main entry point for post-session analysis.
Fetches hand data, extracts decisions, and generates insights.
"""

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from dataclasses import dataclass, field

from .tracker import CompletedSession
from .grading import PreflopGrader, Grade

if TYPE_CHECKING:
    from ..persistence import FirestoreClient

# Import key hands and insight generation
from ..insights.key_hands import select_key_hands, ScoredHand
from ..insights.hand_converter import convert_hand_to_full_insight_request
from ..insights.generator import InsightGenerator
from ..insights.luck import compute_luck_categories, compute_session_luck_score


@dataclass
class Decision:
    """A single decision point extracted from a hand."""
    hand_id: str
    street: str  # preflop, flop, turn, river
    action: str  # fold, check, call, bet, raise
    amount_cents: Optional[int]
    pot_cents: int
    to_call_cents: int
    position: int  # seat relative to button
    stack_cents: int
    # TODO: Add solver comparison fields later
    # gto_frequencies: dict[str, float]
    # ev_by_action: dict[str, float]
    # classification: str  # brilliant, great, good, inaccuracy, mistake, blunder


@dataclass
class SessionAnalysis:
    """Results of session analysis."""
    session_id: str
    user_id: str

    # Basic stats
    hands_played: int
    duration_seconds: int
    profit_cents: int

    # Tendencies (calculated from decisions)
    vpip: float = 0.0  # Voluntarily put in pot %
    pfr: float = 0.0   # Preflop raise %
    af: float = 0.0    # Aggression factor
    cbet: float = 0.0  # Continuation bet %
    wtsd: float = 0.0  # Went to showdown %

    # Decision analysis
    decisions_analyzed: int = 0

    # Preflop grading
    preflop_decisions_graded: int = 0
    preflop_mistakes: int = 0
    preflop_good: int = 0
    preflop_mistake_details: list[dict] = field(default_factory=list)

    # Key hands with AI insights
    key_hand_insights: list[dict] = field(default_factory=list)

    # Luck factor categories - one entry per category that had data.
    luck_categories: list[dict] = field(default_factory=list)

    # Rolled-up 1-100 luck score across categories (None if no category had data).
    # 50 ≈ average session; >50 ran hot; <50 ran cold.
    luck_score: Optional[dict] = None

    analyzed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


async def process_session(
    session: CompletedSession,
    firestore: Optional["FirestoreClient"] = None,
) -> Optional[SessionAnalysis]:
    """
    Process a completed session and generate analysis.

    This is the main entry point called when a session ends.

    Steps:
    1. Fetch hand data from Firestore
    2. Extract decision points for the user
    3. Calculate tendencies (VPIP, PFR, etc.)
    4. [Future] Compare to solver, classify decisions
    5. [Future] Generate AI insights
    6. Store results
    """
    print(f"[PROCESSOR] Starting analysis for session {session.session_id}")
    print(f"[PROCESSOR] User: {session.user_id[:20]}...")
    print(f"[PROCESSOR] Hands: {session.hands_played}, Profit: {session.profit_cents} cents")

    if not session.hand_ids:
        print(f"[PROCESSOR] No hands to analyze")
        return None

    # Step 1: Fetch hand data
    hands = []
    if firestore:
        hands = await _fetch_hands(firestore, session.hand_ids)
        print(f"[PROCESSOR] Fetched {len(hands)} hands from Firestore")

    # Step 2: Extract decisions for this user
    decisions = _extract_decisions(hands, session.user_id)
    print(f"[PROCESSOR] Extracted {len(decisions)} decisions")

    # Step 3: Calculate tendencies
    tendencies = _calculate_tendencies(hands, session.user_id)

    # Step 4: Grade preflop decisions
    preflop_grades = []
    preflop_mistakes = []
    preflop_good = []
    if firestore:
        grader = PreflopGrader(firestore)
        for hand in hands:
            grades = grader.grade_hand(hand, session.user_id)
            preflop_grades.extend(grades)

        # Separate mistakes and good plays
        preflop_mistakes_raw = [g for g in preflop_grades if g.grade == Grade.MISTAKE]
        preflop_good = [g for g in preflop_grades if g.grade == Grade.GOOD]

        # Surface at most one mistake per hand. When the grader flags multiple
        # decisions in the same hand (e.g. a marginal open AND the call vs 3bet
        # that followed), keep the highest-confidence one — it's the most
        # reliable signal and avoids double-counting in the user-facing summary.
        best_per_hand: dict[str, "GradedDecision"] = {}
        for g in preflop_mistakes_raw:
            existing = best_per_hand.get(g.hand_id)
            if existing is None or g.confidence > existing.confidence:
                best_per_hand[g.hand_id] = g
        preflop_mistakes = list(best_per_hand.values())

        # Top-3 by confidence for the user-facing details list. The full
        # `preflop_mistakes` count is preserved as a stat — we just don't
        # render every single error to the user. Sorting descending puts the
        # most clear-cut blunders first.
        preflop_mistakes_top = sorted(
            preflop_mistakes, key=lambda g: g.confidence, reverse=True
        )[:3]

        print(
            f"[PROCESSOR] Graded {len(preflop_grades)} preflop decisions: "
            f"{len(preflop_good)} good, {len(preflop_mistakes_raw)} mistake-actions "
            f"({len(preflop_mistakes)} after per-hand dedup, "
            f"surfacing top {len(preflop_mistakes_top)} by confidence)"
        )

    # Detect format from the first hand we managed to fetch. HU (2 seats) gets
    # routed through HU-tuned AI/grading tooling; 6-max keeps the original flow.
    is_hu = bool(hands) and len(hands[0].get("seats", [])) == 2
    print(f"[PROCESSOR] Format: {'HU' if is_hu else '6-max/multi'} "
          f"(seats in first hand: {len(hands[0].get('seats', [])) if hands else 0})")

    # Step 5: Select key hands and generate AI insights (pro users only)
    key_hand_insights = []
    if hands and session.is_pro:
        key_hand_insights = await _generate_key_hand_insights(
            hands, session.user_id, is_hu=is_hu
        )
        print(f"[PROCESSOR] Generated {len(key_hand_insights)} key hand insights "
              f"({'HU' if is_hu else '6-max'} flow)")
    elif hands:
        print(f"[PROCESSOR] Skipping AI insights — user is not pro")

    # Step 6: Luck factor categories (cheap CPU; always on, all users)
    luck_categories_list: list[dict] = []
    luck_score: Optional[dict] = None
    if hands:
        import asyncio
        try:
            cats = await asyncio.to_thread(compute_luck_categories, hands, session.user_id)
            luck_categories_list = [c.to_dict() for c in cats]
            for c in cats:
                print(f"[PROCESSOR] Luck/{c.category_id}: {c.headline} (n={c.sample_size})")
        except Exception as e:
            print(f"[PROCESSOR] Luck categories failed: {e!r}")

        luck_score = compute_session_luck_score(luck_categories_list)
        if luck_score:
            print(f"[PROCESSOR] Luck score: {luck_score['score']}/100 "
                  f"(z={luck_score['combined_z']}, contributors={[c['category_id'] for c in luck_score['contributors']]})")

    # Build analysis result
    analysis = SessionAnalysis(
        session_id=session.session_id,
        user_id=session.user_id,
        hands_played=session.hands_played,
        duration_seconds=session.duration_seconds,
        profit_cents=session.profit_cents,
        decisions_analyzed=len(decisions),
        preflop_decisions_graded=len(preflop_grades),
        preflop_mistakes=len(preflop_mistakes),
        preflop_good=len(preflop_good),
        preflop_mistake_details=[
            {
                "hand_id": g.hand_id,
                "hand": g.hand,
                "position": g.position,
                # Descriptive label (Open / 3bet / Call vs 3bet / Limp / ...)
                # falls back to the canonical category if unset (legacy graders).
                "action": g.action_label or g.action_taken,
                "reasoning": g.reasoning,
                "confidence": g.confidence,
            }
            for g in preflop_mistakes_top
        ],
        key_hand_insights=key_hand_insights,
        luck_categories=luck_categories_list,
        luck_score=luck_score,
        **tendencies,
    )

    # Step 5: Store results
    if firestore:
        await _store_analysis(firestore, session, analysis)

    print(f"[PROCESSOR] Analysis complete for {session.session_id}")
    print(f"[PROCESSOR] VPIP={analysis.vpip:.1%} PFR={analysis.pfr:.1%} AF={analysis.af:.1f}")

    return analysis


async def _generate_key_hand_insights(
    hands: list[dict],
    user_id: str,
    is_hu: bool = False,
) -> list[dict]:
    """
    Select key hands and generate AI insights for each (in parallel).

    Args:
        hands: List of hand documents
        user_id: Hero's user ID
        is_hu: True for heads-up sessions; routes generator to HU prompts/tooling

    Returns:
        List of insight dicts with hand info and AI-generated insight
    """
    import asyncio

    # Select key hands based on session length
    key_hands = select_key_hands(hands, user_id)

    if not key_hands:
        print("[PROCESSOR] No key hands selected (session too short or no interesting hands)")
        return []

    print(f"[PROCESSOR] Selected {len(key_hands)} key hands for insight generation")

    # Create insight generator (no vector search for speed)
    try:
        generator = InsightGenerator(use_vector_search=False)
    except Exception as e:
        print(f"[PROCESSOR] Failed to create InsightGenerator: {e}")
        return []

    # Prepare all requests first
    tasks = []
    task_metadata = []  # Track which scored_hand each task corresponds to

    for scored_hand in key_hands:
        # Find the full hand data
        hand_data = next((h for h in hands if h.get("hand_id") == scored_hand.hand_id), None)

        if not hand_data:
            print(f"[PROCESSOR] Could not find hand data for {scored_hand.hand_id}")
            continue

        # Convert to insight request
        try:
            request = convert_hand_to_full_insight_request(hand_data, user_id)
            if not request:
                print(f"[PROCESSOR] Could not convert hand {scored_hand.hand_id}")
                continue
        except Exception as e:
            print(f"[PROCESSOR] Error converting hand {scored_hand.hand_id}: {e}")
            continue

        # Create async task for this insight (HU sessions route through HU prompts)
        tasks.append(asyncio.to_thread(
            generator.generate_hand_insight, request, is_hu=is_hu
        ))
        task_metadata.append(scored_hand)

    if not tasks:
        return []

    print(f"[PROCESSOR] Generating {len(tasks)} insights in parallel...")

    # Run all API calls concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Build insight records from results
    insights = []
    for scored_hand, result in zip(task_metadata, results):
        if isinstance(result, Exception):
            print(f"[PROCESSOR] Error generating insight for {scored_hand.hand_id}: {result}")
            insight_text = None
            insight_terms: dict = {}
        else:
            insight_text = result.insight if result else None
            # `terms` maps glossary term_id → exact substring as it appears in
            # the insight text (see TermExtractor in insights/generator.py).
            # iOS reads this off the baked analysis doc (Path A) to render
            # tappable concept tokens; before today's fix this field was
            # produced but discarded, leaving iOS with no terms to highlight.
            insight_terms = result.terms if result else {}
            if insight_text:
                print(f"[PROCESSOR] ✓ {scored_hand.hand_id}: {insight_text[:60]}... "
                      f"(terms: {len(insight_terms)})")

        insight_record = {
            "hand_id": scored_hand.hand_id,
            "score": scored_hand.score,
            "hero_position": scored_hand.hero_position,
            "hero_hand": scored_hand.hero_hand,
            "board": scored_hand.board,
            "profit_bb": scored_hand.profit_bb,
            "pot_type": scored_hand.pot_type,
            "max_street": scored_hand.max_street,
            "insight": insight_text,
            "terms": insight_terms,
        }
        insights.append(insight_record)

    print(f"[PROCESSOR] Generated {len([i for i in insights if i['insight']])} insights")
    return insights


async def _fetch_hands(firestore: "FirestoreClient", hand_ids: list[str]) -> list[dict]:
    """Fetch hand documents from Firestore.

    Uses the batch helper that fans out via asyncio.gather across the
    thread pool. A 100-hand session that previously cost ~5 s of loop
    blocking now completes in roughly one Firestore RTT — and even that
    runs off the event loop thread.
    """
    if not hand_ids:
        return []
    try:
        results = await firestore.get_hands(hand_ids)
    except Exception as e:
        print(f"[PROCESSOR] Error fetching hands batch: {e}")
        return []
    # Drop None entries (hands that no longer exist) while preserving order.
    return [h for h in results if h is not None]


def _extract_decisions(hands: list[dict], user_id: str) -> list[Decision]:
    """Extract decision points for a user from hand data."""
    decisions = []

    for hand in hands:
        actions = hand.get("actions", [])

        # Find user's seat
        user_seat = None
        for seat in hand.get("seats", []):
            if seat.get("user_id") == user_id:
                user_seat = seat.get("seat_index")
                break

        if user_seat is None:
            continue

        # Track game state as we replay actions
        pot = 0
        current_bet = 0
        street = "preflop"

        for action in actions:
            # Update street
            if action.get("street"):
                street = action["street"]
                current_bet = 0

            # Skip if not user's action
            if action.get("seat") != user_seat:
                # Update pot from other players' actions
                amount = action.get("amount", 0)
                if amount:
                    pot += amount
                continue

            action_type = action.get("action", "").lower()
            amount = action.get("amount", 0)

            # This is user's decision point
            decision = Decision(
                hand_id=hand.get("hand_id", ""),
                street=street,
                action=action_type,
                amount_cents=amount if amount else None,
                pot_cents=pot,
                to_call_cents=current_bet,
                position=user_seat,  # TODO: Calculate relative to button
                stack_cents=0,  # TODO: Track stack
            )
            decisions.append(decision)

            # Update pot
            if amount:
                pot += amount

    return decisions


def _calculate_tendencies(hands: list[dict], user_id: str) -> dict:
    """Calculate playing tendencies from hand data."""
    if not hands:
        return {"vpip": 0.0, "pfr": 0.0, "af": 0.0, "cbet": 0.0, "wtsd": 0.0}

    vpip_hands = 0
    pfr_hands = 0
    cbet_opportunities = 0
    cbet_made = 0
    aggressive_actions = 0
    passive_actions = 0
    showdown_hands = 0
    saw_flop_hands = 0

    for hand in hands:
        actions = hand.get("actions", [])

        # Find user's seat
        user_seat = None
        for seat in hand.get("seats", []):
            if seat.get("user_id") == user_id:
                user_seat = seat.get("seat_index")
                break

        if user_seat is None:
            continue

        user_vpip = False
        user_pfr = False
        user_was_preflop_aggressor = False
        user_saw_flop = False
        street = "preflop"

        for action in actions:
            if action.get("street"):
                street = action["street"]

            if action.get("seat") != user_seat:
                continue

            action_type = action.get("action", "").lower()

            # Preflop stats
            if street == "preflop":
                if action_type in ("call", "bet", "raise"):
                    user_vpip = True
                if action_type in ("bet", "raise"):
                    user_pfr = True
                    user_was_preflop_aggressor = True

            # Track if saw flop
            if street == "flop":
                user_saw_flop = True

                # C-bet opportunity
                if user_was_preflop_aggressor and cbet_opportunities == 0:
                    cbet_opportunities += 1
                    if action_type in ("bet", "raise"):
                        cbet_made += 1

            # Aggression tracking (postflop)
            if street in ("flop", "turn", "river"):
                if action_type in ("bet", "raise"):
                    aggressive_actions += 1
                elif action_type in ("call", "check"):
                    passive_actions += 1

        if user_vpip:
            vpip_hands += 1
        if user_pfr:
            pfr_hands += 1
        if user_saw_flop:
            saw_flop_hands += 1

        # Check showdown
        winners = hand.get("winners", [])
        for winner in winners:
            if winner.get("seat") == user_seat and winner.get("cards_shown"):
                showdown_hands += 1
                break

    total_hands = len(hands)

    vpip = vpip_hands / total_hands if total_hands > 0 else 0
    pfr = pfr_hands / total_hands if total_hands > 0 else 0
    af = aggressive_actions / passive_actions if passive_actions > 0 else 0
    cbet = cbet_made / cbet_opportunities if cbet_opportunities > 0 else 0
    wtsd = showdown_hands / saw_flop_hands if saw_flop_hands > 0 else 0

    return {
        "vpip": vpip,
        "pfr": pfr,
        "af": af,
        "cbet": cbet,
        "wtsd": wtsd,
    }


async def _store_analysis(
    firestore: "FirestoreClient",
    session: CompletedSession,
    analysis: SessionAnalysis,
) -> None:
    """Store session and analysis to Firestore.

    When the iOS client supplied a session UUID up-front, this only merges the
    `analysis` subtree into bot_sessions/{client_session_id}, leaving iOS-owned
    metadata (start_time, hand_ids, profit_cents, etc.) untouched. Otherwise it
    falls back to the legacy `sessions/{sess_xxx}` write that owns the full doc.
    """
    analysis_map = {
        "vpip": analysis.vpip,
        "pfr": analysis.pfr,
        "af": analysis.af,
        "cbet": analysis.cbet,
        "wtsd": analysis.wtsd,
        "decisions_analyzed": analysis.decisions_analyzed,
        "preflop_decisions_graded": analysis.preflop_decisions_graded,
        "preflop_mistakes": analysis.preflop_mistakes,
        "preflop_good": analysis.preflop_good,
        "preflop_mistake_details": analysis.preflop_mistake_details,
        "key_hand_insights": analysis.key_hand_insights,
        "luck_categories": analysis.luck_categories,
        "luck_score": analysis.luck_score,
        "analyzed_at": analysis.analyzed_at,
    }

    if session.client_session_id:
        # iOS owns the metadata doc at bot_sessions/{client_session_id}.
        # Merge only the analysis fields so we don't fight the client schema.
        try:
            await firestore.merge_bot_session_analysis(
                session.client_session_id,
                {"analysis": analysis_map},
            )
            print(
                f"[PROCESSOR] Merged analysis into bot_sessions/{session.client_session_id}"
            )
        except Exception as e:
            print(f"[PROCESSOR] Error merging bot_session analysis: {e}")
        return

    # Legacy path: backend-only doc in `sessions/`.
    session_doc = {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "table_id": session.table_id,
        "stake_id": session.stake_id,
        "display_name": session.display_name,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "duration_seconds": session.duration_seconds,
        "hands_played": session.hands_played,
        "hand_ids": session.hand_ids,
        "buy_in_cents": session.buy_in_cents,
        "total_rebuys_cents": session.total_rebuys_cents,
        "rebuy_count": session.rebuy_count,
        "final_chips_cents": session.final_chips_cents,
        "profit_cents": session.profit_cents,
        "analysis": analysis_map,
    }

    try:
        await firestore.write_session(session.session_id, session_doc)
        print(f"[PROCESSOR] Stored session {session.session_id} to Firestore")
    except Exception as e:
        print(f"[PROCESSOR] Error storing session: {e}")
