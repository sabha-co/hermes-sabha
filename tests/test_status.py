from status import DEFAULT, REFRESH_SECONDS, StatusLines


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def lines():
    clock = Clock()
    return StatusLines(clock), clock


ROOM = ("2", None)
THREAD = ("2", "80")


def test_the_first_refresh_says_thinking_and_an_unchanged_one_sends_nothing():
    status, _ = lines()
    assert status.due(THREAD, None) == DEFAULT
    status.sent(THREAD, DEFAULT)
    assert status.due(THREAD, None) is None


def test_a_new_phrase_goes_out_at_once():
    status, _ = lines()
    status.sent(THREAD, DEFAULT)
    assert status.due(THREAD, "  Searching\nthe web… ") == "Searching the web…"


def test_an_unchanged_line_is_sent_again_before_sabha_would_clear_it():
    status, clock = lines()
    status.sent(THREAD, DEFAULT)
    clock.now = REFRESH_SECONDS
    assert status.due(THREAD, None) == DEFAULT


def test_after_a_post_there_no_refresh_puts_the_status_back_until_the_turn_ends():
    status, _ = lines()
    status.sent(THREAD, DEFAULT)
    status.posted(THREAD)
    assert status.due(THREAD, "Still going") is None
    assert status.finish("2") == []
    assert status.due(THREAD, None) == DEFAULT


def test_the_end_of_a_turn_clears_only_the_statuses_still_showing_in_that_room():
    status, _ = lines()
    status.sent(THREAD, DEFAULT)
    status.sent(ROOM, DEFAULT)
    status.sent(("9", None), DEFAULT)
    assert status.finish("2", "80") == [THREAD]
    assert sorted(status.finish("2"), key=str) == [ROOM]
    assert status.finish("2") == []


def test_a_sender_about_to_be_turned_away_gets_no_status():
    status, _ = lines()
    status.quiet("2")
    assert status.due(THREAD, None) is None
    status.finish("2")
    assert status.due(THREAD, None) == DEFAULT
