from findme.identity import asset_id_for


def test_asset_id_is_stable_url_safe_and_derived_from_identity() -> None:
    first = asset_id_for("dnd", "Tokens/Goblin Warrior.png")
    second = asset_id_for("dnd", "Tokens/Goblin Warrior.png")
    other_source = asset_id_for("other", "Tokens/Goblin Warrior.png")

    assert first == second
    assert first != other_source
    assert len(first) == 22
    assert first.replace("-", "").replace("_", "").isalnum()
