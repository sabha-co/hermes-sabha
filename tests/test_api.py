import asyncio

from api import SabhaApi, SabhaApiError
import pytest


class Response:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload
        self.content_type, self.headers, self.reason = "application/json", {}, ""

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Answers each request with the next response, recording what was sent."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, **kwargs):
        self.requests.append(kwargs)
        return Response(*self.responses.pop(0))


POSTED = (201, {"id": 7, "room_id": 5})
REFUSED = (422, {"error": "client_message_id is not permitted", "code": "validation_failed"})


def post(session, **kwargs):
    return asyncio.run(SabhaApi(session, "https://chat.test/api/v1", "key").post_message("5", "<p>hi</p>", **kwargs))


def test_a_post_carries_its_client_message_id():
    session = Session(POSTED)
    assert post(session, client_message_id="id-1").id == "7"
    assert session.requests[0]["json"] == {"body": "<p>hi</p>", "client_message_id": "id-1"}


def test_a_file_carries_it_as_a_form_field(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("hello")
    session = Session(POSTED)
    asyncio.run(SabhaApi(session, "https://chat.test/api/v1", "key").post_file("5", str(file), client_message_id="id-1"))
    fields = {field[0]["name"]: field[2] for field in session.requests[0]["data"]._fields}
    assert fields["client_message_id"] == "id-1"


def test_a_server_that_refuses_the_key_gets_the_post_without_it_from_then_on():
    session = Session(REFUSED, POSTED, POSTED)
    api = SabhaApi(session, "https://chat.test/api/v1", "key")
    asyncio.run(api.post_message("5", "<p>hi</p>", client_message_id="id-1"))
    asyncio.run(api.post_message("5", "<p>again</p>", client_message_id="id-2"))
    assert [r["json"] for r in session.requests] == [
        {"body": "<p>hi</p>", "client_message_id": "id-1"}, {"body": "<p>hi</p>"}, {"body": "<p>again</p>"}]


def test_any_other_refusal_is_raised():
    session = Session((422, {"error": "Message can't be blank", "code": "validation_failed"}))
    with pytest.raises(SabhaApiError):
        post(session, client_message_id="id-1")
    assert len(session.requests) == 1


def test_a_status_names_its_place_and_a_clear_takes_the_thread_as_a_parameter():
    session = Session((200, {"draft_id": "d-1"}), (204, {}))
    api = SabhaApi(session, "https://chat.test/api/v1", "key")
    asyncio.run(api.set_status("5", "Thinking…", parent_message_id="80"))
    asyncio.run(api.clear_status("5", parent_message_id="80"))
    assert session.requests[0]["json"] == {"status": "Thinking…", "parent_message_id": "80"}
    assert session.requests[1]["params"] == {"parent_message_id": "80"}


def test_a_refused_command_list_names_the_commands_another_app_holds():
    refused = (422, {"error": "Commands refused", "code": "validation_failed",
                     "refusals": [{"name": "new", "error": "is already another app's command"}]})
    api = SabhaApi(Session(refused, (200, {})), "https://chat.test/api/v1", "key")
    assert asyncio.run(api.declare_commands([{"name": "new"}, {"name": "model"}])) == ["new"]
    assert asyncio.run(api.declare_commands([{"name": "model"}])) == []
