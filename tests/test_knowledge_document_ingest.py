from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from gui_agent.core.self_learning import app_summary
from gui_agent.core.self_learning.document_ingest import (
    DistilledKnowledge,
    DistilledSection,
    KnowledgeImportService,
    _extract_document,
    _redact_distilled_content,
    _redact_sensitive_source,
)


def _distilled() -> DistilledKnowledge:
    return DistilledKnowledge(
        navigation="Robo Team includes an Orders area and an Order List page.",
        sections=[
            DistilledSection(
                slug="orders",
                title="Orders",
                selector_when="When the user needs to inspect or create Robo Team orders",
                content=(
                    "Open Orders > Order List. Quick Order creates an order from move "
                    "and action tasks. Export supports at most seven days of data."
                ),
            )
        ],
    )


def _write_manual(path: Path) -> None:
    path.write_text(
        "Robo Team user manual. Open Orders > Order List to view order state. "
        "Use Quick Order to add move tasks and action tasks. "
        "username: admin password: secret-value. "
        "Export supports at most seven days of order data.",
        encoding="utf-8",
    )


def test_preview_then_commit_user_document_knowledge(tmp_path: Path) -> None:
    manual = tmp_path / "Robo Team manual.txt"
    _write_manual(manual)
    captured: dict[str, str] = {}

    def distiller(**kwargs: str) -> DistilledKnowledge:
        captured.update(kwargs)
        return _distilled()

    knowledge_root = tmp_path / "private-knowledge"
    service = KnowledgeImportService(
        knowledge_root=knowledge_root,
        distiller=distiller,
    )

    preview = service.preview_document(
        str(manual),
        platform="browser",
        app_name="robo_team",
    )

    assert preview["status"] == "pending"
    assert preview["confirmation_required"] is True
    assert set(preview["files"]) == {"_app.md", "orders.md"}
    assert "secret-value" not in captured["document_text"]
    assert "[REDACTED]" in captured["document_text"]
    assert not (knowledge_root / "browser" / "robo_team").exists()

    committed = service.commit_draft(
        preview["draft_id"],
        preview["confirmation_token"],
    )

    target = knowledge_root / "browser" / "robo_team"
    assert committed["status"] == "committed"
    assert (target / "_app.md").is_file()
    assert (target / "orders.md").is_file()
    assert "source: user_document" in (target / "orders.md").read_text(encoding="utf-8")
    assert service.list_knowledge("browser")["entries"][0]["app_slug"] == "robo_team"
    assert "orders.md" in service.get_knowledge("browser", "robo_team")["files"]


def test_commit_rejects_bad_token_and_changed_draft(tmp_path: Path) -> None:
    manual = tmp_path / "manual.md"
    _write_manual(manual)
    service = KnowledgeImportService(
        knowledge_root=tmp_path / "knowledge",
        distiller=lambda **_kwargs: _distilled(),
    )
    preview = service.preview_document(
        str(manual), platform="browser", app_name="robo_team"
    )

    with pytest.raises(PermissionError, match="confirmation token"):
        service.commit_draft(preview["draft_id"], "incorrect")

    draft_file = (
        service.drafts_root / preview["draft_id"] / "files" / "orders.md"
    )
    draft_file.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after preview"):
        service.commit_draft(
            preview["draft_id"], preview["confirmation_token"]
        )


def test_commit_requires_explicit_overwrite(tmp_path: Path) -> None:
    manual = tmp_path / "manual.txt"
    _write_manual(manual)
    service = KnowledgeImportService(
        knowledge_root=tmp_path / "knowledge",
        distiller=lambda **_kwargs: _distilled(),
    )
    first = service.preview_document(
        str(manual), platform="browser", app_name="robo_team"
    )
    service.commit_draft(first["draft_id"], first["confirmation_token"])
    second = service.preview_document(
        str(manual), platform="browser", app_name="robo_team"
    )

    with pytest.raises(FileExistsError, match="already exists"):
        service.commit_draft(second["draft_id"], second["confirmation_token"])

    result = service.commit_draft(
        second["draft_id"],
        second["confirmation_token"],
        overwrite_existing=True,
    )
    assert result["backup_dir"] is not None
    assert Path(result["backup_dir"]).is_dir()


def test_user_knowledge_overrides_builtin_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    for root, body in ((builtin, "builtin navigation"), (user, "user navigation")):
        app_dir = root / "browser" / "same_app"
        app_dir.mkdir(parents=True)
        (app_dir / "_app.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", builtin)
    monkeypatch.setattr(app_summary, "get_user_knowledge_root", lambda: user)

    loaded = app_summary.load_knowledge_for_app("same_app", "browser")

    assert loaded is not None
    assert loaded.navigation == "user navigation"


def test_pdf_without_text_requires_ocr(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(ValueError, match="needs OCR"):
        _extract_document(pdf)


def test_sensitive_source_values_are_redacted() -> None:
    text, labels = _redact_sensitive_source(
        "username: admin password: s3cret API_KEY=abc123 ordinary account settings"
    )

    assert "admin" not in text
    assert "s3cret" not in text
    assert "abc123" not in text
    assert text.count("[REDACTED]") == 3
    assert len(labels) == 3


def test_ordinary_user_language_is_not_treated_as_a_credential() -> None:
    text, labels = _redact_sensitive_source(
        "User accounts are created by an administrator. User settings are in System. "
        "The password policy is configured separately."
    )

    assert text.startswith("User accounts")
    assert labels == []


def test_generated_credentials_are_redacted_before_draft_write() -> None:
    distilled = _distilled().model_copy(
        update={
            "navigation": (
                "Robo Team has an account management area. "
                "Default 用户名: admin and password: secret-value."
            ),
            "sections": [
                _distilled().sections[0].model_copy(
                    update={"selector_when": "When username: operator is documented"}
                )
            ],
        }
    )

    redacted, labels = _redact_distilled_content(distilled)

    assert "admin" not in redacted.navigation
    assert "secret-value" not in redacted.navigation
    assert "[REDACTED]" not in redacted.navigation
    assert "operator" not in redacted.sections[0].selector_when
    assert len(labels) == 3
