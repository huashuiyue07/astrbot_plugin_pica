"""排行榜订阅存储测试（core/subscriptions.py）。"""

import json

from core.subscriptions import RankSubscriptionStore

UMO_A = "default:group:100"
UMO_B = "default:group:200"


def test_add_and_read(tmp_path):
    store = RankSubscriptionStore(tmp_path)

    assert store.add(UMO_A, "H24", "u1") is True

    assert store.targets(UMO_A) == ["H24"]
    assert store.owner_of(UMO_A) == "u1"
    assert store.groups_for("H24") == [UMO_A]
    assert store.path.exists()


def test_add_is_idempotent_but_updates_owner(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "H24", "u1")

    assert store.add(UMO_A, "H24", "u2") is False
    assert store.targets(UMO_A) == ["H24"]
    assert store.owner_of(UMO_A) == "u2"


def test_targets_are_normalized_case_and_order(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "d30", "u1")
    store.add(UMO_A, "h24", "u1")
    store.add(UMO_A, " D7 ", "u1")

    assert store.targets(UMO_A) == ["H24", "D7", "D30"]


def test_invalid_rank_type_is_rejected(tmp_path):
    store = RankSubscriptionStore(tmp_path)

    assert store.add(UMO_A, "XX", "u1") is False

    assert store.targets(UMO_A) == []
    assert store.groups_for("XX") == []


def test_groups_for_returns_sorted_sessions(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_B, "H24")
    store.add(UMO_A, "H24")
    store.add(UMO_A, "D7")

    assert store.groups_for("H24") == [UMO_A, UMO_B]
    assert store.groups_for("D7") == [UMO_A]
    assert store.groups_for("D30") == []


def test_groups_for_reads_file_once(tmp_path):
    """无论有多少订阅会话，groups_for 只读一次盘（此前是 n+1 次）"""
    store = RankSubscriptionStore(tmp_path)
    for index in range(5):
        store.add(f"default:group:{index}", "H24")

    reads = []
    real_read = store._read

    def _counting_read():
        reads.append(1)
        return real_read()

    store._read = _counting_read

    assert len(store.groups_for("H24")) == 5
    assert len(reads) == 1


def test_remove_single_target_keeps_others(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "H24")
    store.add(UMO_A, "D7")

    removed = store.remove(UMO_A, "H24")

    assert removed == ["H24"]
    assert store.targets(UMO_A) == ["D7"]


def test_remove_all_drops_record(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "H24")
    store.add(UMO_A, "D7")

    removed = store.remove(UMO_A)

    assert removed == ["H24", "D7"]
    assert store.all() == {}
    assert store.groups_for("H24") == []


def test_remove_unknown_session_returns_empty(tmp_path):
    store = RankSubscriptionStore(tmp_path)

    assert store.remove("nobody:group:1") == []
    assert store.remove("nobody:group:1", "H24") == []


def test_remove_unknown_target_keeps_record(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "H24")

    assert store.remove(UMO_A, "D7") == []
    assert store.targets(UMO_A) == ["H24"]


def test_persisted_across_instances(tmp_path):
    RankSubscriptionStore(tmp_path).add(UMO_A, "D30", "u9")

    assert RankSubscriptionStore(tmp_path).targets(UMO_A) == ["D30"]
    assert RankSubscriptionStore(tmp_path).owner_of(UMO_A) == "u9"


def test_corrupt_file_is_tolerated(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.path.write_text("{ 坏掉的 json", encoding="utf-8")

    assert store.all() == {}
    assert store.add(UMO_A, "H24") is True
    assert json.loads(store.path.read_text(encoding="utf-8"))[UMO_A]["targets"] == ["H24"]


def test_unknown_targets_in_file_are_filtered(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.path.write_text(
        json.dumps({UMO_A: {"targets": ["H24", "BOGUS"]}, "bad": "not-a-dict"}),
        encoding="utf-8",
    )

    assert store.targets(UMO_A) == ["H24"]
    assert store.all() == {UMO_A: {"targets": ["H24", "BOGUS"]}}


def test_non_list_targets_are_ignored(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.path.write_text(
        json.dumps({UMO_A: {"targets": "H24"}}), encoding="utf-8"
    )

    assert store.targets(UMO_A) == []


def test_write_leaves_no_temp_file(tmp_path):
    store = RankSubscriptionStore(tmp_path)
    store.add(UMO_A, "H24")

    assert list(tmp_path.glob("*.tmp")) == []
