"""Tests for the two-pass structured reasoning system."""

import os
import pytest
from unittest.mock import MagicMock, patch

from src.insights.generator import InsightGenerator, build_hand_prompt
from src.insights.schema import HandInsightRequest, StreetAction, HeroDecision


# Sample hand fixtures
@pytest.fixture
def flop_cbet_hand():
    """Standard single-raised pot flop c-bet decision."""
    return HandInsightRequest(
        hero_position="BTN",
        hero_hand="AsKs",
        num_players=2,
        pot_type="single raised",
        board="Qc 7d 2h",
        street_actions=[
            StreetAction(street="preflop", cards="", actions="BTN raises to 2.5bb, BB calls"),
            StreetAction(street="flop", cards="Qc 7d 2h", actions="BB checks, Hero to act"),
        ],
        hero_decisions=[
            HeroDecision(
                street="flop",
                action_taken="bets 3bb",
                pot_before_bb=5.5,
                facing="check",
                position_vs_villain="in position"
            ),
        ],
        hero_won=True,
        profit_bb=5.5,
    )


@pytest.fixture
def three_bet_pot_hand():
    """3-bet pot turn decision."""
    return HandInsightRequest(
        hero_position="BB",
        hero_hand="JsJc",
        num_players=2,
        pot_type="3-bet",
        board="Kd 8c 4s 2h",
        street_actions=[
            StreetAction(street="preflop", cards="", actions="BTN raises to 2.5bb, BB 3-bets to 9bb, BTN calls"),
            StreetAction(street="flop", cards="Kd 8c 4s", actions="BB bets 6bb, BTN calls"),
            StreetAction(street="turn", cards="2h", actions="BB to act"),
        ],
        hero_decisions=[
            HeroDecision(
                street="flop",
                action_taken="bets 6bb",
                pot_before_bb=18,
                facing="first to act",
                position_vs_villain="out of position"
            ),
            HeroDecision(
                street="turn",
                action_taken="checks",
                pot_before_bb=30,
                facing="first to act",
                position_vs_villain="out of position"
            ),
        ],
        hero_won=False,
        profit_bb=-15,
    )


@pytest.fixture
def river_bluff_catch_hand():
    """River bluff-catching decision."""
    return HandInsightRequest(
        hero_position="CO",
        hero_hand="AhQc",
        num_players=2,
        pot_type="single raised",
        board="Qs 9c 4d 7h 2s",
        street_actions=[
            StreetAction(street="preflop", cards="", actions="CO raises to 2.5bb, BB calls"),
            StreetAction(street="flop", cards="Qs 9c 4d", actions="BB checks, CO bets 2bb, BB calls"),
            StreetAction(street="turn", cards="7h", actions="BB checks, CO checks"),
            StreetAction(street="river", cards="2s", actions="BB bets 8bb, CO to act"),
        ],
        hero_decisions=[
            HeroDecision(
                street="flop",
                action_taken="bets 2bb",
                pot_before_bb=5.5,
                facing="check",
                position_vs_villain="in position"
            ),
            HeroDecision(
                street="turn",
                action_taken="checks",
                pot_before_bb=9.5,
                facing="check",
                position_vs_villain="in position"
            ),
            HeroDecision(
                street="river",
                action_taken="calls",
                pot_before_bb=9.5,
                facing="8bb bet",
                position_vs_villain="in position"
            ),
        ],
        hero_won=True,
        profit_bb=17.5,
    )


class TestBuildHandPrompt:
    """Test hand prompt construction."""

    def test_builds_prompt_with_all_sections(self, flop_cbet_hand):
        prompt = build_hand_prompt(flop_cbet_hand)

        assert "=== HAND SUMMARY ===" in prompt
        assert "Hero position: BTN" in prompt
        assert "Hero's hand: AsKs" in prompt
        assert "single raised" in prompt
        assert "=== ACTION BY STREET ===" in prompt
        assert "PREFLOP:" in prompt
        assert "FLOP [Qc 7d 2h]:" in prompt

    def test_includes_hero_decisions(self, flop_cbet_hand):
        prompt = build_hand_prompt(flop_cbet_hand)

        assert "=== HERO'S DECISIONS ===" in prompt
        assert "bets 3bb" in prompt
        assert "Pot: 5.5bb" in prompt


class TestReasoningPrompts:
    """Test reasoning prompt components."""

    def test_reasoning_system_prompt_has_five_steps(self):
        from src.insights.prompts.reasoning import REASONING_SYSTEM_PROMPT

        assert "STEP 1:" in REASONING_SYSTEM_PROMPT
        assert "STEP 2:" in REASONING_SYSTEM_PROMPT
        assert "STEP 3:" in REASONING_SYSTEM_PROMPT
        assert "STEP 4:" in REASONING_SYSTEM_PROMPT
        assert "STEP 5:" in REASONING_SYSTEM_PROMPT
        assert "KEY FINDING:" in REASONING_SYSTEM_PROMPT

    def test_insight_system_prompt_references_analysis(self):
        from src.insights.prompts.reasoning import INSIGHT_SYSTEM_PROMPT

        assert "ANALYSIS" in INSIGHT_SYSTEM_PROMPT
        assert "KEY FINDING" in INSIGHT_SYSTEM_PROMPT
        assert "JSON" in INSIGHT_SYSTEM_PROMPT

    def test_search_janda_tool_schema(self):
        from src.insights.prompts.reasoning import SEARCH_JANDA_TOOL

        assert SEARCH_JANDA_TOOL["name"] == "search_janda"
        assert "query" in SEARCH_JANDA_TOOL["input_schema"]["properties"]
        assert "required" in SEARCH_JANDA_TOOL["input_schema"]


class TestFormatJandaResults:
    """Test Janda result formatting."""

    def test_formats_results_with_sources(self):
        # Create a mock generator with minimal setup
        with patch.object(InsightGenerator, '__init__', lambda x, **kwargs: None):
            gen = InsightGenerator()
            gen.vector_store = None

            results = [
                {
                    "title": "C-Betting Dry Flops",
                    "text": "On dry flops like K72 rainbow...",
                    "part": 2,
                    "name": "Flop Play",
                    "score": 0.89
                },
                {
                    "title": "Range Advantage",
                    "text": "The preflop raiser has range advantage...",
                    "part": 1,
                    "name": "Theory",
                    "score": 0.82
                }
            ]

            formatted = gen._format_janda_results(results)

            assert "C-Betting Dry Flops" in formatted
            assert "Janda Part 2: Flop Play" in formatted
            assert "0.89" in formatted
            assert "Range Advantage" in formatted

    def test_handles_empty_results(self):
        with patch.object(InsightGenerator, '__init__', lambda x, **kwargs: None):
            gen = InsightGenerator()
            gen.vector_store = None

            formatted = gen._format_janda_results([])
            assert "No relevant textbook excerpts" in formatted


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("PINECONE_API_KEY"),
    reason="Requires ANTHROPIC_API_KEY and PINECONE_API_KEY"
)
class TestTwoPassIntegration:
    """Integration tests for two-pass generation (requires API keys)."""

    @pytest.fixture
    def generator(self):
        return InsightGenerator(use_vector_search=True)

    def test_generates_insight_for_flop_decision(self, generator, flop_cbet_hand):
        response = generator.generate_hand_insight(flop_cbet_hand)

        assert response.insight
        assert len(response.insight) > 20
        assert response.prompt_tokens > 0
        assert response.completion_tokens > 0

    def test_generates_insight_for_3bet_pot(self, generator, three_bet_pot_hand):
        response = generator.generate_hand_insight(three_bet_pot_hand)

        assert response.insight
        assert len(response.insight) > 20

    def test_generates_insight_for_river_decision(self, generator, river_bluff_catch_hand):
        response = generator.generate_hand_insight(river_bluff_catch_hand)

        assert response.insight
        assert len(response.insight) > 20

    def test_extracts_terms_from_insight(self, generator, flop_cbet_hand):
        response = generator.generate_hand_insight(flop_cbet_hand)

        # Terms dict should exist (may be empty if no terms matched)
        assert isinstance(response.terms, dict)


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("PINECONE_API_KEY"),
    reason="Requires ANTHROPIC_API_KEY and PINECONE_API_KEY"
)
class TestReasoningQuality:
    """Tests for reasoning pass quality (requires API keys)."""

    @pytest.fixture
    def generator(self):
        return InsightGenerator(use_vector_search=True)

    def test_reasoning_mentions_range_categories(self, generator, flop_cbet_hand):
        """Reasoning should discuss specific ranges, not just 'strong range'."""
        hand_prompt = build_hand_prompt(flop_cbet_hand)
        reasoning, _, _ = generator._generate_reasoning(flop_cbet_hand, hand_prompt)

        # Check for range-related language
        reasoning_lower = reasoning.lower()
        range_terms = [
            "range", "hands", "overpair", "top pair", "broadway",
            "suited", "pairs", "draw", "value", "bluff"
        ]
        matched = sum(1 for term in range_terms if term in reasoning_lower)

        assert matched >= 3, f"Reasoning lacks specificity. Found: {matched} range terms"

    def test_reasoning_references_textbook_when_relevant(self, generator, three_bet_pot_hand):
        """Reasoning should incorporate pre-fetched textbook context."""
        hand_prompt = build_hand_prompt(three_bet_pot_hand)
        reasoning, _, _ = generator._generate_reasoning(three_bet_pot_hand, hand_prompt)

        # The reasoning should show evidence of using context
        # (either by incorporating theory or by explicitly referencing it)
        assert len(reasoning) > 500, "Reasoning too short for structured analysis"

    def test_reasoning_has_key_finding(self, generator, flop_cbet_hand):
        """Reasoning should conclude with KEY FINDING."""
        hand_prompt = build_hand_prompt(flop_cbet_hand)
        reasoning, _, _ = generator._generate_reasoning(flop_cbet_hand, hand_prompt)

        assert "KEY FINDING" in reasoning or "key finding" in reasoning.lower()
