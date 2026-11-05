from app.detect.keywords import DEFAULT_HYPE_KEYWORDS, has_hype_keyword, keyword_pattern, parse_keywords


def test_parse_keywords_trims_dedupes_and_falls_back():
    assert parse_keywords(" geil, GEIL, lol ") == ("geil", "lol")
    assert parse_keywords("") == parse_keywords(DEFAULT_HYPE_KEYWORDS)


def test_keyword_pattern_uses_word_boundaries():
    pattern = keyword_pattern(("geil", "lol"))
    assert has_hype_keyword("das war geil", pattern)
    assert has_hype_keyword("LOL", pattern)
    assert not has_hype_keyword("geiler", pattern)


def test_keyword_pattern_matches_emoji_as_literal():
    pattern = keyword_pattern(parse_keywords("😂, geil"))
    assert has_hype_keyword("das ist so 😂", pattern)
    assert not has_hype_keyword("geilheit", pattern)


def test_parse_keywords_falls_back_when_only_separators():
    assert parse_keywords(" , ") == parse_keywords(DEFAULT_HYPE_KEYWORDS)


def test_keyword_pattern_treats_umlaut_keywords_as_word_tokens():
    pattern = keyword_pattern(("böse",))
    assert has_hype_keyword("das ist böse", pattern)
    assert not has_hype_keyword("bösewicht", pattern)
