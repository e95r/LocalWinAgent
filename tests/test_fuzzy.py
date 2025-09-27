from intent_router import IntentRouter


def test_fuzzy_match_detects_notepad():
    router = IntentRouter()
    result = router.fuzzy_match("открой пожалуйста блокнот", router.APP_KEYWORDS)
    assert result == "notepad"


def test_fuzzy_match_threshold():
    router = IntentRouter()
    result = router.fuzzy_match("совсем не похоже", router.APP_KEYWORDS)
    assert result is None
