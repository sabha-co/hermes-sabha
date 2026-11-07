from retries import HOLD_SECONDS, MAX_HELD, RetryIds


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_a_retried_send_gets_the_ids_its_failed_attempt_held():
    ids = RetryIds()
    first = ids.take("send", 2)
    ids.hold("send", first)
    assert ids.take("send", 2) == first


def test_a_send_that_succeeded_or_changed_gets_new_ids():
    ids = RetryIds()
    first = ids.take("send", 1)
    assert ids.take("send", 1) != first
    ids.hold("send", first)
    assert ids.take("other", 1) != first
    assert ids.take("send", 2)[0] != first[0]


def test_held_ids_expire_and_are_capped():
    clock = Clock()
    ids = RetryIds(clock)
    held = ids.take("send", 1)
    ids.hold("send", held)
    clock.now = HOLD_SECONDS
    assert ids.take("send", 1) != held

    ids.hold("oldest", held)
    for n in range(MAX_HELD):
        ids.hold(n, ["x"])
    assert ids.take("oldest", 1) != held
