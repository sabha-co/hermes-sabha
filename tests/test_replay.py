from replay import OVERLAP_SECONDS, Replay

SERVER = "https://chat.example.com/api/v1"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_first_ever_connection_asks_for_nothing(tmp_path):
    assert Replay(tmp_path / "replay.json", SERVER).history() is None


def test_a_restarted_gateway_asks_for_what_came_after_the_last_handled_event(tmp_path):
    path, clock = tmp_path / "replay.json", Clock()
    first = Replay(path, SERVER, clock)
    first.subscribed()
    first.handled("app_events:2", "e1", 4)
    first.handled("app_events:2", "e1", 3)  # an older offset never moves it back
    clock.now += 30
    first.disconnected()

    second = Replay(path, SERVER, clock)
    assert second.history() == {"streams": {"app_events:2": {"epoch": "e1", "offset": 4}},
                                "since": int(clock.now) - OVERLAP_SECONDS}


def test_a_reconnect_asks_since_it_last_subscribed():
    clock = Clock()
    replay = Replay(None, SERVER, clock)
    replay.subscribed()
    assert replay.history() == {"streams": {}, "since": int(clock.now) - OVERLAP_SECONDS}


def test_saved_state_from_another_sabha_is_ignored(tmp_path):
    path = tmp_path / "replay.json"
    Replay(path, SERVER).handled("app_events:2", "e1", 4)
    assert Replay(path, "https://other.example.com/api/v1").history() is None


def test_an_unwritable_or_unreadable_file_leaves_replay_in_memory(tmp_path):
    (tmp_path / "replay.json").write_text("not json")
    replay = Replay(tmp_path / "missing-dir" / "replay.json", SERVER)
    replay.handled("app_events:2", "e1", 4)
    assert replay.history() == {"streams": {"app_events:2": {"epoch": "e1", "offset": 4}}}
    assert Replay(tmp_path / "replay.json", SERVER).history() is None


def test_forgetting_the_positions_lets_a_restarted_stream_count_from_one(tmp_path):
    path = tmp_path / "replay.json"
    replay = Replay(path, SERVER)
    replay.handled("app_events:2", "e1", 4)
    replay.forget()
    replay.handled("app_events:2", "e1", 1)
    assert Replay(path, SERVER).history()["streams"] == {"app_events:2": {"epoch": "e1", "offset": 1}}
