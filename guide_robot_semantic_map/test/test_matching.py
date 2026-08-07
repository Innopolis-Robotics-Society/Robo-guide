"""Юниты на fuzzy-резолв алиасов."""

from __future__ import annotations

from guide_robot_semantic_map.lib.matching import Match, is_confident, resolve, score
from guide_robot_semantic_map.lib.text_norm import normalize

# -- score() ------------------------------------------------------------------


def test_score_exact_match_is_one() -> None:
    assert score("кандинский", "кандинский") == 1.0


def test_score_empty_inputs_are_zero() -> None:
    assert score("", "кандинский") == 0.0
    assert score("кандинский", "") == 0.0
    assert score("", "") == 0.0


def test_score_prefix_scores_between_zero_and_one() -> None:
    s = score("кандинск", "кандинский")
    assert 0.0 < s < 1.0


def test_score_short_prefix_below_min_len_not_boosted() -> None:
    # "ка" короче _PREFIX_MIN_LEN=4 -- не должно давать искусственный буст.
    s = score("ка", "кандинский")
    assert s < 0.5


def test_score_typo_still_scores_reasonably_high() -> None:
    s = score("кандинсий", "кандинский")
    assert s > 0.8


def test_score_unrelated_words_score_low() -> None:
    s = score("лидар", "кандинский")
    assert s < 0.3


def test_score_is_symmetric_for_prefix_case() -> None:
    # Не гарантируется дизайном явно, но обе стороны -- валидные вызовы,
    # и обе обязаны быть в (0, 1) без ValueError/исключений.
    assert 0.0 < score("вход", "главный вход") <= 1.0
    assert 0.0 < score("главный вход", "вход") <= 1.0


# -- resolve() ------------------------------------------------------------------


_LOCATIONS = {
    "entrance": {"ru": ["вход", "главный вход"], "en": ["entrance", "main entrance"]},
    "kandinsky_viii": {
        "ru": ["кандинский", "композиция восемь", "композиция viii"],
        "en": ["kandinsky", "composition eight"],
    },
    "cafe": {"ru": ["кафе", "буфет"], "en": ["cafe"]},
}


def test_resolve_exact_match_top_result() -> None:
    matches = resolve("кандинский", _LOCATIONS)
    assert matches[0].location_id == "kandinsky_viii"
    assert matches[0].score == 1.0


def test_resolve_empty_query_returns_empty() -> None:
    assert resolve("", _LOCATIONS) == []
    assert resolve("   ", _LOCATIONS) == []


def test_resolve_language_filter_restricts_aliases() -> None:
    # "cafe" -- английский алиас "cafe"; по-русски его не найти, если
    # запрашивать только en, а запрос на кириллице.
    matches = resolve("кафе", _LOCATIONS, language="en")
    assert not any(m.location_id == "cafe" for m in matches)


def test_resolve_language_all_by_default() -> None:
    matches = resolve("kandinsky", _LOCATIONS)
    assert matches[0].location_id == "kandinsky_viii"


def test_resolve_respects_max_results() -> None:
    matches = resolve("а", _LOCATIONS, max_results=1)
    assert len(matches) <= 1


def test_resolve_max_results_zero_uses_default() -> None:
    matches = resolve("кандинский", _LOCATIONS, max_results=0)
    assert len(matches) <= 5


def test_resolve_drops_zero_score_stopword_only_query() -> None:
    assert resolve("покажи", _LOCATIONS) == []


def test_resolve_returns_one_match_per_location() -> None:
    matches = resolve("композиция", _LOCATIONS)
    kandinsky_hits = [m for m in matches if m.location_id == "kandinsky_viii"]
    assert len(kandinsky_hits) == 1


def test_resolve_sorted_descending() -> None:
    matches = resolve("вход", _LOCATIONS)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_resolve_matched_alias_is_reported() -> None:
    matches = resolve("кандинский", _LOCATIONS)
    top = next(m for m in matches if m.location_id == "kandinsky_viii")
    assert normalize(top.matched_alias) == "кандинский"


# -- is_confident() ------------------------------------------------------------


def test_is_confident_empty_scores() -> None:
    assert is_confident([]) is False


def test_is_confident_single_score_above_threshold() -> None:
    assert is_confident([0.9]) is True


def test_is_confident_single_score_below_threshold() -> None:
    assert is_confident([0.4]) is False


def test_is_confident_clear_winner() -> None:
    assert is_confident([0.95, 0.5]) is True


def test_is_confident_close_scores_not_confident() -> None:
    assert is_confident([0.7, 0.68]) is False


def test_is_confident_custom_threshold_and_margin() -> None:
    assert is_confident([0.5, 0.2], threshold=0.4, margin=0.2) is True
    assert is_confident([0.5, 0.2], threshold=0.6, margin=0.2) is False


def test_match_is_a_plain_dataclass() -> None:
    match = Match(location_id="entrance", score=1.0, matched_alias="вход")
    assert match.location_id == "entrance"
