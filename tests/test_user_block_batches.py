import pytest

from comdi_sync.eventicious import EventiciousError
from tests.test_speakers import schedule_with


@pytest.mark.parametrize("count", [0, 1, 199, 200, 201, 324, 400, 401])
def test_missing_users_respect_api_limit(count, make_syncer, api, state, monkeypatch):
    source_ids = tuple(range(1, count + 1))
    syncer = make_syncer(schedule_with(*source_ids))
    assert syncer.run_once()["failed"] == 0
    requests = []
    original = api.update_users

    def limited_update(body):
        requests.append(list(body["users"]))
        if len(body["users"]) > 200:
            raise EventiciousError("PATCH", "users", 400, "Max number of entries per request: 200")
        return original(body)

    monkeypatch.setattr(api, "update_users", limited_update)
    syncer.comdi.schedule = schedule_with()
    stats = syncer.run_once()
    assert stats["failed"] == 0
    assert [len(batch) for batch in requests] == [min(200, count - i) for i in range(0, count, 200)]
    assert stats["changes"].get("user_block", 0) == count
    assert all(state.is_blocked("user", sid) for sid in source_ids)
    assert all(api.users[state.target_id("user", sid)]["withdrawn"] for sid in source_ids)
    requests.clear()
    assert syncer.run_once()["failed"] == 0
    assert requests == []


def test_failed_second_batch_retries_only_unconfirmed_users(make_syncer, api, state, monkeypatch):
    source_ids = tuple(range(1, 325))
    syncer = make_syncer(schedule_with(*source_ids))
    assert syncer.run_once()["failed"] == 0
    requests = []
    confirmed = set()
    original = api.update_users

    def fail_second(body):
        users = body["users"]
        requests.append(len(users))
        if len(users) > 200:
            raise EventiciousError("PATCH", "users", 400, "Max number of entries per request: 200")
        if len(requests) == 2:
            raise EventiciousError("PATCH", "users", 400, "second batch rejected")
        result = original(body)
        confirmed.update(user["id"] for user in users)
        return result

    monkeypatch.setattr(api, "update_users", fail_second)
    syncer.comdi.schedule = schedule_with()
    stats = syncer.run_once()
    assert requests == [200, 124]
    assert stats["failed"] == 1
    assert stats["changes"]["user_block"] == 200
    assert sum(state.is_blocked("user", sid) for sid in source_ids) == 200
    assert {state.target_id("user", sid) for sid in source_ids if state.is_blocked("user", sid)} == confirmed
    stats = syncer.run_once()
    assert stats["failed"] == 0
    assert requests == [200, 124, 124]
    assert stats["changes"]["user_block"] == 124
    assert all(state.is_blocked("user", sid) for sid in source_ids)
