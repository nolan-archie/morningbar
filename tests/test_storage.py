import json

from morningbar.storage import Store


def test_save_for_later_keeps_story_metadata_local(tmp_path):
    store = Store(tmp_path / "morningbar.sqlite3")

    store.save_for_later(
        "story-1",
        "A useful headline",
        "https://example.com/story",
        "linux",
        ["Source A", "Source B"],
    )

    with store.connect() as db:
        saved = db.execute("SELECT * FROM saved_stories WHERE story_id = ?", ("story-1",)).fetchone()

    assert saved["title"] == "A useful headline"
    assert saved["link"] == "https://example.com/story"
    assert saved["category_id"] == "linux"
    assert json.loads(saved["source_names"]) == ["Source A", "Source B"]
    assert saved["saved_at"]
