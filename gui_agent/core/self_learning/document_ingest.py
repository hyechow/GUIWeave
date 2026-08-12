"""Preview and commit user documents as local GUIWeave application knowledge."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.platforms import PLATFORMS, PlatformName
from gui_agent.core.self_learning.paths import get_user_knowledge_root
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured


SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt"}
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_CHARS = 180_000
MAX_PDF_PAGES = 500
_APP_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SECTION_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_SYSTEM_PROMPT = load_prompt_text("task.knowledge.document_ingest")
_CREDENTIAL_VALUE_RE = r"([a-z0-9_.@+/-]{2,200})"
_SENSITIVE_PATTERNS = (
    re.compile(
        rf"(?i)(user[ _-]?name)\s*(?::|=|\bis\b)\s*{_CREDENTIAL_VALUE_RE}"
    ),
    re.compile(
        rf"(?i)(password|passwd|pwd)\s*(?::|=|\bis\b)\s*{_CREDENTIAL_VALUE_RE}"
    ),
    re.compile(
        rf"(用户名|用戶名|登录账号|登入帳號)\s*[:：=为是]?\s*{_CREDENTIAL_VALUE_RE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(密码|密碼)\s*[:：=为是]?\s*{_CREDENTIAL_VALUE_RE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|cookie)"
        rf"\s*(?::|=|\bis\b)\s*{_CREDENTIAL_VALUE_RE}"
    ),
)


class DistilledSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    selector_when: str = Field(min_length=8, max_length=300)
    content: str = Field(min_length=20, max_length=20_000)


class DistilledKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    navigation: str = Field(min_length=20, max_length=20_000)
    sections: list[DistilledSection] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def _unique_section_slugs(self) -> "DistilledKnowledge":
        slugs = [section.slug for section in self.sections]
        if len(slugs) != len(set(slugs)):
            raise ValueError("section slugs must be unique")
        return self


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slugify_app(value: str) -> str:
    slug = _APP_SLUG_RE.sub("_", value.strip().lower()).strip("_")
    if not slug or len(slug) > 64:
        raise ValueError("app_name must contain a short ASCII name")
    return slug


def _safe_section_slug(value: str) -> str:
    slug = _SECTION_SLUG_RE.sub("_", value.strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        raise ValueError(f"invalid section slug: {value!r}")
    return slug[:64].rstrip("_")


def _display_name_from_path(path: Path) -> str:
    stem = re.sub(r"(?i)(使用说明书|用户手册|操作手册|manual|guide)$", "", path.stem)
    return stem.strip(" _-–—") or path.stem


def _redact_sensitive_source(text: str) -> tuple[str, list[str]]:
    redacted = text
    labels: list[str] = []
    for pattern in _SENSITIVE_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            label = match.group(1)
            if label not in labels:
                labels.append(label)
            return f"{label}: [REDACTED]"

        redacted = pattern.sub(replace, redacted)
    return redacted, labels


def _validate_distilled_content(distilled: DistilledKnowledge) -> None:
    content = "\n".join(
        [
            distilled.navigation,
            *(
                field
                for section in distilled.sections
                for field in (section.title, section.selector_when, section.content)
            ),
        ]
    )
    for pattern in _SENSITIVE_PATTERNS:
        match = pattern.search(content)
        if match and match.group(2) != "[REDACTED]":
            raise ValueError(
                f"generated knowledge contains possible credential material near {match.group(1)!r}"
            )


def _omit_redacted_sentences(text: str) -> str:
    """Drop sentences that still describe credential material after redaction."""

    cleaned = re.sub(
        r"[^\n。！？.!?]*\[REDACTED\][^\n。！？.!?]*[。！？.!?]?",
        "",
        text,
    )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _redact_distilled_content(
    distilled: DistilledKnowledge,
) -> tuple[DistilledKnowledge, list[str]]:
    navigation, labels = _redact_sensitive_source(distilled.navigation)
    navigation = _omit_redacted_sentences(navigation)
    all_labels = list(labels)
    sections: list[DistilledSection] = []
    for section in distilled.sections:
        title, title_labels = _redact_sensitive_source(section.title)
        selector_when, selector_labels = _redact_sensitive_source(
            section.selector_when
        )
        content, section_labels = _redact_sensitive_source(section.content)
        content = _omit_redacted_sentences(content)
        for label in (*title_labels, *selector_labels, *section_labels):
            if label not in all_labels:
                all_labels.append(label)
        sections.append(
            section.model_copy(
                update={
                    "title": title,
                    "selector_when": selector_when,
                    "content": content,
                }
            )
        )
    redacted = distilled.model_copy(
        update={"navigation": navigation, "sections": sections}
    )
    return DistilledKnowledge.model_validate(redacted.model_dump()), all_labels


def _extract_pdf(path: Path) -> tuple[str, int, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging contract
        raise RuntimeError("PDF import requires the pypdf package") from exc

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("encrypted PDF cannot be read without a password") from exc
        if not unlocked:
            raise ValueError("encrypted PDF cannot be read without a password")
    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError("PDF contains no pages")
    if page_count > MAX_PDF_PAGES:
        raise ValueError(f"PDF has {page_count} pages; maximum is {MAX_PDF_PAGES}")

    pages: list[str] = []
    low_text_pages = 0
    for index, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"failed to extract PDF page {index}: {exc}") from exc
        if len(text) < 20:
            low_text_pages += 1
        if text:
            pages.append(f"\n\n[Page {index}]\n{text}")
    extracted = "".join(pages).strip()
    if len(extracted) < 80 or low_text_pages >= max(1, int(page_count * 0.8)):
        raise ValueError(
            "PDF contains too little extractable text and likely needs OCR; "
            "this preview does not ingest scanned pages"
        )
    warnings: list[str] = []
    if low_text_pages:
        warnings.append(f"{low_text_pages} of {page_count} pages contained little text")
    return extracted, page_count, warnings


def _extract_document(path: Path) -> tuple[str, int | None, list[str]]:
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"document exceeds the {MAX_SOURCE_BYTES // 1024 // 1024} MB limit")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"unsupported document type {suffix!r}; expected {supported}")
    if suffix == ".pdf":
        text, pages, warnings = _extract_pdf(path)
    else:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown and text documents must be UTF-8") from exc
        pages = None
        warnings = []
        if len(text) < 80:
            raise ValueError("document contains too little text to create knowledge")
    text = "".join(
        char for char in text if char in "\n\t" or ord(char) >= 32
    )
    if len(text) > MAX_DOCUMENT_CHARS:
        text = text[:MAX_DOCUMENT_CHARS]
        warnings.append(
            f"extracted text was limited to the first {MAX_DOCUMENT_CHARS} characters"
        )
    return text, pages, warnings


def _default_distiller(
    *,
    document_text: str,
    platform: PlatformName,
    app_name: str,
    source_name: str,
) -> DistilledKnowledge:
    cfg = resolve_llm_config("knowledge.ingest")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
    )
    payload = {
        "target": {"platform": platform, "app_name": app_name},
        "source_name": source_name,
        "document_data": document_text,
    }
    return invoke_structured(
        llm,
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ],
        DistilledKnowledge,
        trace_label="knowledge.document_ingest",
    )


def _frontmatter(
    *,
    identifier: str,
    source_type: str,
    platform: PlatformName,
    app_slug: str,
    source_name: str,
    source_sha256: str,
    selector_when: str | None = None,
) -> str:
    lines = [
        "---",
        f"id: {identifier}",
        f"source_type: {source_type}",
        f"platform: {platform}",
        f"app: {app_slug}",
        "scope:",
        "  - orchestrator",
        "  - statement",
    ]
    if selector_when:
        lines.append(f"selector_when: {selector_when.replace(chr(10), ' ').strip()}")
    lines.extend(
        [
            "source: user_document",
            f"source_file: {json.dumps(source_name, ensure_ascii=False)}",
            f"source_sha256: {source_sha256}",
            "confidence: medium",
            "sensitivity: private",
            "ttl: session",
            "version: 1",
            "---",
        ]
    )
    return "\n".join(lines)


def _render_files(
    distilled: DistilledKnowledge,
    *,
    platform: PlatformName,
    app_name: str,
    app_slug: str,
    source_name: str,
    source_sha256: str,
) -> dict[str, str]:
    app_meta = _frontmatter(
        identifier=f"knowledge.{platform}.{app_slug}.navigation",
        source_type="knowledge_navigation",
        platform=platform,
        app_slug=app_slug,
        source_name=source_name,
        source_sha256=source_sha256,
    )
    files = {"_app.md": f"{app_meta}\n# {app_name.strip()}\n\n{distilled.navigation.strip()}\n"}
    for section in distilled.sections:
        slug = _safe_section_slug(section.slug)
        metadata = _frontmatter(
            identifier=f"knowledge.{platform}.{app_slug}.{slug}",
            source_type="knowledge_section",
            platform=platform,
            app_slug=app_slug,
            source_name=source_name,
            source_sha256=source_sha256,
            selector_when=section.selector_when,
        )
        files[f"{slug}.md"] = (
            f"{metadata}\n# {section.title.strip()}\n\n{section.content.strip()}\n"
        )
    return files


def _draft_digest(files_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files_dir.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class KnowledgeImportService:
    """Own local knowledge drafts, confirmation tokens and atomic commits."""

    def __init__(
        self,
        *,
        knowledge_root: Path | None = None,
        distiller: Callable[..., DistilledKnowledge] | None = None,
    ) -> None:
        self.knowledge_root = (knowledge_root or get_user_knowledge_root()).resolve()
        self._distiller = distiller or _default_distiller

    @property
    def drafts_root(self) -> Path:
        return self.knowledge_root / ".drafts"

    def preview_document(
        self,
        file_path: str,
        *,
        platform: PlatformName,
        app_name: str | None = None,
    ) -> dict[str, Any]:
        source_path = Path(file_path).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"document not found: {source_path}")
        display_name = (app_name or _display_name_from_path(source_path)).strip()
        app_slug = _slugify_app(display_name)
        source_sha256 = _sha256_file(source_path)
        document_text, page_count, warnings = _extract_document(source_path)
        safe_document_text, redacted_labels = _redact_sensitive_source(document_text)
        if redacted_labels:
            warnings.append(
                "credential-like source text was redacted before knowledge generation"
            )
        distilled = self._distiller(
            document_text=safe_document_text,
            platform=platform,
            app_name=display_name,
            source_name=source_path.name,
        )
        distilled, output_redactions = _redact_distilled_content(distilled)
        if output_redactions:
            warnings.append(
                "credential-like generated text was redacted before the draft was written"
            )
        _validate_distilled_content(distilled)
        files = _render_files(
            distilled,
            platform=platform,
            app_name=display_name,
            app_slug=app_slug,
            source_name=source_path.name,
            source_sha256=source_sha256,
        )

        draft_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        token = secrets.token_urlsafe(24)
        draft_dir = self.drafts_root / draft_id
        files_dir = draft_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            (files_dir / name).write_text(content, encoding="utf-8")
        digest = _draft_digest(files_dir)
        manifest = {
            "draft_id": draft_id,
            "status": "pending",
            "platform": platform,
            "app_name": display_name,
            "app_slug": app_slug,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "name": source_path.name,
                "path": str(source_path),
                "sha256": source_sha256,
                "pages": page_count,
                "extracted_chars": len(document_text),
            },
            "warnings": warnings,
            "file_names": sorted(files),
            "draft_digest": digest,
            "confirmation_token_hash": _sha256_bytes(token.encode("utf-8")),
        }
        (draft_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            **self.get_draft(draft_id),
            "confirmation_token": token,
            "confirmation_required": True,
            "commit_instruction": (
                "Show this preview to the user. Call commit_knowledge_draft only after "
                "the user explicitly confirms it in a subsequent message."
            ),
        }

    def _load_manifest(self, draft_id: str) -> tuple[Path, dict[str, Any]]:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", draft_id):
            raise ValueError("invalid draft_id")
        draft_dir = (self.drafts_root / draft_id).resolve()
        if self.drafts_root.resolve() not in draft_dir.parents:
            raise ValueError("draft_id resolves outside the drafts root")
        manifest_path = draft_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"unknown knowledge draft: {draft_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return draft_dir, manifest

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft_dir, manifest = self._load_manifest(draft_id)
        files_dir = draft_dir / "files"
        previews = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(files_dir.glob("*.md"))
        }
        return {
            "draft_id": draft_id,
            "status": manifest["status"],
            "platform": manifest["platform"],
            "app_name": manifest["app_name"],
            "app_slug": manifest["app_slug"],
            "source": manifest["source"],
            "warnings": manifest["warnings"],
            "files": previews,
        }

    def commit_draft(
        self,
        draft_id: str,
        confirmation_token: str,
        *,
        overwrite_existing: bool = False,
    ) -> dict[str, Any]:
        draft_dir, manifest = self._load_manifest(draft_id)
        if manifest.get("status") != "pending":
            raise ValueError(f"knowledge draft is already {manifest.get('status')}")
        supplied_hash = _sha256_bytes(confirmation_token.encode("utf-8"))
        if not secrets.compare_digest(supplied_hash, manifest["confirmation_token_hash"]):
            raise PermissionError("confirmation token does not match this knowledge draft")
        files_dir = draft_dir / "files"
        if _draft_digest(files_dir) != manifest["draft_digest"]:
            raise ValueError("knowledge draft changed after preview; create a new preview")

        platform = manifest["platform"]
        app_slug = manifest["app_slug"]
        target = self.knowledge_root / platform / app_slug
        if target.exists() and not overwrite_existing:
            raise FileExistsError(
                f"knowledge already exists for {platform}/{app_slug}; "
                "preview again and explicitly allow overwrite"
            )
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        staging_root = self.knowledge_root / ".commits"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"{draft_id}-{secrets.token_hex(3)}"
        shutil.copytree(files_dir, staging)
        backup_path: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup_root = self.knowledge_root / ".backups" / platform
                backup_root.mkdir(parents=True, exist_ok=True)
                backup_path = backup_root / f"{app_slug}-{draft_id}"
                target.replace(backup_path)
            staging.replace(target)
        except Exception:
            if backup_path is not None and backup_path.exists() and not target.exists():
                backup_path.replace(target)
            raise

        manifest["status"] = "committed"
        manifest["committed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["target"] = str(target)
        manifest["backup"] = str(backup_path) if backup_path else None
        (draft_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "status": "committed",
            "draft_id": draft_id,
            "platform": platform,
            "app_slug": app_slug,
            "knowledge_dir": str(target),
            "files": sorted(path.name for path in target.glob("*.md")),
            "backup_dir": str(backup_path) if backup_path else None,
        }

    def list_knowledge(self, platform: PlatformName | None = None) -> dict[str, Any]:
        platforms = [platform] if platform else list(PLATFORMS)
        entries: list[dict[str, Any]] = []
        for platform_name in platforms:
            platform_dir = self.knowledge_root / platform_name
            if not platform_dir.is_dir():
                continue
            for app_dir in sorted(platform_dir.iterdir()):
                if not app_dir.is_dir() or not (app_dir / "_app.md").is_file():
                    continue
                entries.append(
                    {
                        "platform": platform_name,
                        "app_slug": app_dir.name,
                        "knowledge_dir": str(app_dir),
                        "files": sorted(path.name for path in app_dir.glob("*.md")),
                    }
                )
        return {"knowledge_root": str(self.knowledge_root), "entries": entries}

    def get_knowledge(self, platform: PlatformName, app_slug: str) -> dict[str, Any]:
        normalized = _slugify_app(app_slug)
        app_dir = (self.knowledge_root / platform / normalized).resolve()
        platform_dir = (self.knowledge_root / platform).resolve()
        if platform_dir not in app_dir.parents or not (app_dir / "_app.md").is_file():
            raise FileNotFoundError(f"unknown user knowledge: {platform}/{normalized}")
        return {
            "platform": platform,
            "app_slug": normalized,
            "knowledge_dir": str(app_dir),
            "files": {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted(app_dir.glob("*.md"))
            },
        }


__all__ = [
    "DistilledKnowledge",
    "DistilledSection",
    "KnowledgeImportService",
    "get_user_knowledge_root",
]
