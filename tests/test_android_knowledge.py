from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID_KNOWLEDGE = ROOT / "knowledge" / "android"


def test_android_app_knowledge_is_complete_and_declarative() -> None:
    files = {path.parent.name: path for path in ANDROID_KNOWLEDGE.glob("*/_app.md")}

    assert set(files) == {"Calendar", "Files", "Mastodon", "Messages", "Taodian"}
    forbidden = (
        "ctx.", "success=", "values=", "fields=", "filters=", "coverage=",
        "atomic_role=", "rows[", "in Python", "Planning boundary",
    )
    for path in files.values():
        text = path.read_text(encoding="utf-8")
        assert not [token for token in forbidden if token in text], path
