"""Runtime contracts shared by statement executors and the DSL interpreter.

Executors produce values; this module validates those values against the declaration on the
statement.  It contains no GUI execution, retry, or re-planning policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field


_EMPTY_RETURN_OK_CUES = (
    "留空",
    "未选中",
    "selectedindex=-1",
    "unselected",
    "no selection",
    "empty allowed",
    "allow empty",
)


def _is_query_run(run: object) -> bool:
    """Whether the statement is a deterministic read/data query."""
    return bool(getattr(run, "is_query", False))


def _compact_text(text: str) -> str:
    return "".join(ch.lower() for ch in str(text or "") if not ch.isspace())


def _read_spec_fragments(text: str) -> list[str]:
    spec = str(text or "")
    for sep in ("；", ";", "\n", "。"):
        spec = spec.replace(sep, "\n")
    return [frag.strip() for frag in spec.splitlines() if frag.strip()]


def ui_return_field_allows_empty(run: object, field: str) -> bool:
    """Whether this return field explicitly treats blank as a valid value."""
    field_key = _compact_text(field)
    if not field_key:
        return False
    for fragment in _read_spec_fragments(getattr(run, "read_spec", "") or ""):
        compact = _compact_text(fragment)
        if field_key not in compact:
            continue
        if any(_compact_text(cue) in compact for cue in _EMPTY_RETURN_OK_CUES):
            return True
    return False


def missing_ui_return_fields(run: object, reads: dict[str, str]) -> list[str]:
    """Return declared interactive-result fields that were not actually read."""
    if run is None or not getattr(run, "returns", None) or _is_query_run(run):
        return []
    missing: list[str] = []
    for field in getattr(run, "returns", []):
        field_name = str(field)
        if str(reads.get(field_name, "")).strip():
            continue
        if field_name in reads and ui_return_field_allows_empty(run, field_name):
            continue
        missing.append(field_name)
    return missing


_DOMAIN_URL_NAME_RE = re.compile(r"url|href|链接", re.IGNORECASE)
_DOMAIN_NUMBER_NAME_RE = re.compile(
    r"count|total|amount|price|数量|计数|金额|条数|行数|总数|数目",
    re.IGNORECASE,
)
_DOMAIN_DATE_NAME_RE = re.compile(r"\bdate\b|\btime\b|日期|时间", re.IGNORECASE)
_HAS_DIGIT_RE = re.compile(r"\d")

_ENUM_SUCCESS_WORDS = {
    "success", "successful", "succeeded", "saved", "done", "ok", "okay", "passed", "true", "yes",
    "成功", "已成功", "保存成功", "已保存", "完成", "已完成", "通过", "是",
}
_ENUM_FAILURE_WORDS = {
    "fail", "failed", "failure", "error", "invalid", "false", "no",
    "失败", "保存失败", "错误", "报错", "无效", "未完成", "否",
}


def _enum_options(domain: str) -> list[str]:
    kind = str(domain or "").strip()
    if not kind.lower().startswith("enum:"):
        return []
    return [opt.strip() for opt in kind.split(":", 1)[1].split("|") if opt.strip()]


def _enum_alias_matches(compact: str, alias: str) -> bool:
    if not alias:
        return False
    if alias.isascii() and len(alias) <= 3:
        return compact == alias
    return compact == alias or alias in compact


def _enum_bucket(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    compact = re.sub(r"[\s_\-:：,，.!！。]+", "", text)
    for word in _ENUM_SUCCESS_WORDS:
        key = re.sub(r"[\s_\-:：,，.!！。]+", "", word.casefold())
        if _enum_alias_matches(compact, key):
            return "success"
    for word in _ENUM_FAILURE_WORDS:
        key = re.sub(r"[\s_\-:：,，.!！。]+", "", word.casefold())
        if _enum_alias_matches(compact, key):
            return "failure"
    return ""


def canonicalize_return_value(value: str, domain: str) -> str:
    """Canonicalize clear success/failure aliases to a declared enum option."""
    text = str(value or "").strip()
    options = _enum_options(domain)
    if not text or not options:
        return text
    for option in options:
        if text.casefold() == option.casefold():
            return option
    bucket = _enum_bucket(text)
    if not bucket:
        return text
    for option in options:
        if _enum_bucket(option) == bucket:
            return option
    return text


def _value_fits_domain(value: str, domain: str) -> tuple[bool, str]:
    """Return whether a value fits ``url|number|date|enum:...|text``."""
    text = str(value).strip()
    kind = domain.strip().lower()
    if kind.startswith("enum:"):
        options = _enum_options(domain)
        normalized = {opt.casefold() for opt in options}
        if canonicalize_return_value(text, domain).casefold() in normalized:
            return True, ""
        return False, f"不在枚举域 {options} 内"
    if kind == "url":
        if " " not in text and ("://" in text or "/" in text):
            return True, ""
        return False, "不是 URL/路径形态"
    if kind == "number":
        if _HAS_DIGIT_RE.search(text):
            return True, ""
        return False, "不含任何数字，不是数值/计数"
    if kind == "date":
        if _HAS_DIGIT_RE.search(text):
            return True, ""
        return False, "不含任何数字，不是日期/时间"
    return True, ""


def _inferred_domain(field_name: str) -> str:
    if _DOMAIN_URL_NAME_RE.search(field_name):
        return "url"
    if _DOMAIN_NUMBER_NAME_RE.search(field_name):
        return "number"
    if _DOMAIN_DATE_NAME_RE.search(field_name):
        return "date"
    return ""


@dataclass
class DomainViolation:
    """One non-empty return value outside its declared or inferred domain."""

    field: str
    value: str
    domain: str
    reason: str

    def describe(self) -> str:
        return f"字段「{self.field}」读到「{self.value}」：{self.reason}（要求域 {self.domain}）"


def out_of_domain_return_fields(run: object, reads: dict[str, str]) -> list[DomainViolation]:
    """Check every non-empty declared return value against its domain."""
    if run is None or not getattr(run, "returns", None) or _is_query_run(run):
        return []
    declared = {
        str(k): str(v) for k, v in (getattr(run, "return_domains", None) or {}).items()
    }
    violations: list[DomainViolation] = []
    for field_name in (str(f) for f in getattr(run, "returns", [])):
        value = str(reads.get(field_name, "")).strip()
        if not value:
            continue
        domain = declared.get(field_name) or _inferred_domain(field_name)
        if not domain:
            continue
        fits, reason = _value_fits_domain(value, domain)
        if not fits:
            violations.append(
                DomainViolation(field=field_name, value=value, domain=domain, reason=reason)
            )
    return violations


def normalize_return_reads(run: object, reads: dict[str, str]) -> dict[str, str]:
    """Apply typed-return canonicalization before contract checks and branching."""
    if run is None or not getattr(run, "returns", None) or not reads:
        return dict(reads or {})
    declared = {
        str(k): str(v) for k, v in (getattr(run, "return_domains", None) or {}).items()
    }
    out = dict(reads)
    for field_name in (str(f) for f in getattr(run, "returns", [])):
        if field_name not in out:
            continue
        domain = declared.get(field_name) or _inferred_domain(field_name)
        if domain:
            out[field_name] = canonicalize_return_value(str(out.get(field_name, "")), domain)
    return out


@dataclass
class ReturnContractReport:
    """The return-contract verdict for one completed interactive statement."""

    missing: list[str] = dc_field(default_factory=list)
    out_of_domain: list[DomainViolation] = dc_field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.missing or self.out_of_domain)

    @property
    def violated_fields(self) -> list[str]:
        return list(self.missing) + [v.field for v in self.out_of_domain]

    def describe(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append("实际读取结果为空：" + "、".join(self.missing))
        parts.extend(violation.describe() for violation in self.out_of_domain)
        return "；".join(parts)


def check_return_contract(run: object, reads: dict[str, str]) -> ReturnContractReport:
    """Validate an executor result against the statement's declared outputs."""
    return ReturnContractReport(
        missing=missing_ui_return_fields(run, reads),
        out_of_domain=out_of_domain_return_fields(run, reads),
    )
