from routing import Route, inbound_route, is_from_self, message_text, thread_context


def event(room_type="Open", members=12, thread=None, message_id=10, room_id=5):
    return {
        "event": "message_created",
        "user": {"id": 1, "name": "Alice", "role": "member"},
        "room": {"id": room_id, "name": "General", "type": room_type, "members": members},
        "message": {"id": message_id, "body": {"markdown": "**hi** @Hermes", "plain": "hi @Hermes"},
                    "thread": thread},
    }


def test_room_mention_opens_a_thread_on_the_message_or_stays_in_the_room():
    assert inbound_route(event(), reply_in_thread=True) == Route("5", "channel", "10")
    assert inbound_route(event("Closed"), reply_in_thread=False) == Route("5", "channel", None)


def test_thread_message_is_addressed_by_its_parent_room_and_message():
    thread = {"id": 42, "parent_message_id": 10, "parent_room_id": 5}
    route = inbound_route(event("Thread", thread=thread, message_id=99, room_id=42), reply_in_thread=False)
    assert route == Route("5", "channel", "10")


def test_dms_never_thread_and_a_group_dm_is_a_group():
    assert inbound_route(event("Direct", members=2), reply_in_thread=True) == Route("5", "dm", None)
    assert inbound_route(event("Direct", members=4), reply_in_thread=True) == Route("5", "group", None)


def test_forum_post_without_a_parent_message_stays_flat():
    thread = {"id": 7, "parent_message_id": None, "parent_room_id": 3}
    assert inbound_route(event("Post", thread=thread, room_id=7), reply_in_thread=True) == Route("7", "channel", None)


def test_own_messages_and_text():
    assert is_from_self(event(), "1")
    assert not is_from_self(event(), "")
    assert message_text(event()) == "**hi** @Hermes"


def test_thread_context_shows_the_parent_and_earlier_replies_oldest_first():
    parent = {"id": 10, "creator": {"id": 1, "name": "Ann"}, "body": {"markdown": "Deploy is failing"}}
    replies = [  # newest first, as Sabha pages them
        {"id": 14, "creator": {"id": 3, "name": "Bo"}, "body": {"markdown": "Fixed it myself"}},
        {"id": 13, "creator": {"id": 1, "name": "Ann"}, "body": {"markdown": "@Hermes any idea?"}},
        {"id": 12, "creator": {"id": 2, "name": "Hermes"}, "body": {"markdown": "Looking"}},
        {"id": 11, "creator": {"id": 3, "name": "Bo"}, "body": {"plain": "Since 9am"}},
    ]

    context = thread_context(parent, replies, current_id=13, app_user_id="2")

    assert context.splitlines()[1:4] == ["[thread parent] Ann: Deploy is failing", "Bo: Since 9am", "[assistant] Looking"]
    assert "any idea" not in context and "Fixed it" not in context


def test_thread_context_is_none_when_nothing_has_text():
    assert thread_context({"body": {}}, [], current_id=1, app_user_id="2") is None
