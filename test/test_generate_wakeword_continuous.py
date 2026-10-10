from collections import defaultdict

from scripts.generate_wakeword_continuous import make_plan


def test_full_phrase_pairs_cover_voices_tempos_positions_without_leaking_families():
    plan = make_plan()
    assert len(plan["samples"]) == 256
    assert len({sample["id"] for sample in plan["samples"]}) == 256
    families = defaultdict(set)
    pairs = defaultdict(list)
    for sample in plan["samples"]:
        assert sample["text"].lower().count("фирая") == int(sample["kind"] == "positive")
        families[sample["text_family"]].add(sample["training_split"])
        p = sample["parameters"]
        pairs[sample["text_family"], p["speaker"], p["rate_percent"]].append(sample["kind"])
    assert all(len(splits) == 1 for splits in families.values())
    assert all(sorted(kinds) == ["negative", "positive"] for kinds in pairs.values())
    assert {sample["target_position"] for sample in plan["samples"]} == {"begin", "middle", "end"}
