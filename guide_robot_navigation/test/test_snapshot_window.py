from guide_robot_navigation.snapshot_window import Record, SnapshotWindow

S = 1_000_000_000


def rec(t_s: float, topic: str = "/scan") -> Record:
    return Record(topic=topic, t_ns=int(t_s * S), data=b"x")


def times(snapshot):
    return [r.t_ns / S for r in snapshot.records]


def test_ring_keeps_only_pre_window():
    w = SnapshotWindow(pre_s=2.0, post_s=1.0, max_s=10.0)
    for t in range(10):
        w.add(rec(t))
    w.trigger(now_ns=9 * S, reason="recovery")
    for t in (9.5, 10.0):
        w.add(rec(t))
    snap = w.poll(now_ns=10 * S)
    assert times(snap) == [7, 8, 9, 9.5, 10.0]
    assert snap.reason == "recovery"
    assert snap.triggers == 1


def test_no_snapshot_before_post_window_ends():
    w = SnapshotWindow(pre_s=1.0, post_s=5.0, max_s=10.0)
    w.trigger(now_ns=0, reason="recovery")
    assert w.poll(now_ns=4 * S) is None
    assert w.capturing
    assert w.poll(now_ns=5 * S) is not None
    assert not w.capturing


def test_retrigger_extends_same_snapshot():
    w = SnapshotWindow(pre_s=1.0, post_s=5.0, max_s=60.0)
    assert w.trigger(now_ns=0, reason="recovery") is True
    assert w.trigger(now_ns=4 * S, reason="recovery") is False
    assert w.poll(now_ns=6 * S) is None
    snap = w.poll(now_ns=9 * S)
    assert snap.triggers == 2
    assert snap.end_ns == 9 * S


def test_retrigger_capped_by_max_s():
    w = SnapshotWindow(pre_s=1.0, post_s=5.0, max_s=12.0)
    w.trigger(now_ns=0, reason="recovery")
    for t in range(1, 30):
        w.trigger(now_ns=t * S, reason="recovery")
        if w.poll(now_ns=t * S) is not None:
            assert t == 12
            return
    raise AssertionError("snapshot never closed")


def test_ring_refills_after_snapshot():
    w = SnapshotWindow(pre_s=2.0, post_s=1.0, max_s=10.0)
    w.trigger(now_ns=0, reason="recovery")
    w.add(rec(0.5))
    assert times(w.poll(now_ns=1 * S)) == [0.5]
    for t in (2, 3, 4):
        w.add(rec(t))
    w.trigger(now_ns=4 * S, reason="recovery")
    assert times(w.poll(now_ns=5 * S)) == [2, 3, 4]
