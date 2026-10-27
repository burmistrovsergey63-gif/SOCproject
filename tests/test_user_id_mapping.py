from tests.test_speakers import schedule_with


def remap_user(state, api, source_id, target_id):
    old_id = state.target_id("user", source_id)
    api.users[target_id] = {**api.users.pop(old_id), "id": target_id}
    state._conn.execute(
        "UPDATE objects SET target_id=? WHERE entity='user' AND source_id=?",
        (target_id, source_id),
    )
    state._conn.commit()


def test_persisted_id_used_for_profile_session_block_and_reactivate(make_syncer, state, api):
    syncer = make_syncer(schedule_with(100, episode_speakers=(100,)))
    assert syncer.run_once()["failed"] == 0
    remap_user(state, api, 100, 2004057)
    api.clear_ops()
    assert syncer.run_once()["failed"] == 0
    assert state.target_id("user", 100) == 2004057
    assert api.sessions[state.target_id("session", 10)]["speakersIds"] == [2004057]
    assert not any(op == "create_user" for op, _ in api.ops)
    assert state.get_payload("user", 100)["id"] == 2004057
    api.clear_ops()
    assert syncer.run_once()["failed"] == 0
    assert not any("user" in op for op, _ in api.ops)
    syncer.comdi.schedule = schedule_with()
    assert syncer.run_once()["failed"] == 0
    assert state.is_blocked("user", 100)
    assert api.users[2004057]["withdrawn"] is True
    syncer.comdi.schedule = schedule_with(100)
    assert syncer.run_once()["failed"] == 0
    assert not state.is_blocked("user", 100)
    assert api.users[2004057]["withdrawn"] is False


def test_historical_alias_does_not_block_active_target(make_syncer, state, api):
    syncer = make_syncer(schedule_with(100, 200))
    assert syncer.run_once()["failed"] == 0
    old_id = state.target_id("user", 100)
    active_id = state.target_id("user", 200)
    state._conn.execute("UPDATE objects SET target_id=? WHERE entity='user' AND source_id=100", (active_id,))
    state._conn.commit()
    api.users.pop(old_id)
    syncer.comdi.schedule = schedule_with(100)
    stats = syncer.run_once()
    assert stats["failed"] == 0
    assert not api.users[active_id].get("withdrawn", False)
    assert stats["changes"].get("user_block", 0) == 0
