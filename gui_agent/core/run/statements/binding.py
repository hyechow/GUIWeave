"""Deterministic bindings from the current observation into Program values."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from pydantic import JsonValue

from gui_agent.core.run.contracts import (
    ObservationBinding,
    OutputSpec,
    Read,
    StatementInvocation,
    matches_output_spec,
)
from gui_agent.core.run.observation_materializer import (
    NormalizedObservation,
    materialize_observation,
)
from gui_agent.core.schemas import Observation, StatementOutcome

from .compute_kernel import ComputeKernelError, json_value, normalize_table_rows


class ObservationUnavailable(ComputeKernelError):
    """The declared fact is not present in the current observation."""


@dataclass(frozen=True)
class _BoundFact:
    value: JsonValue
    evidence: str


def _key(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value).strip().casefold())


def _coerce(value: Any, spec: OutputSpec) -> JsonValue:
    value = json_value(value)
    if spec.type == "number" and isinstance(value, str):
        try:
            parsed = float(value.replace(",", ""))
            return int(parsed) if parsed.is_integer() else parsed
        except ValueError:
            return value
    if spec.type == "boolean" and isinstance(value, str):
        if value.casefold() in {"true", "yes", "1", "是"}:
            return True
        if value.casefold() in {"false", "no", "0", "否"}:
            return False
    return value


def _one(candidates: list[_BoundFact], binding: ObservationBinding) -> _BoundFact:
    unique: dict[str, _BoundFact] = {}
    for candidate in candidates:
        fingerprint = json.dumps(candidate.value, ensure_ascii=False, sort_keys=True)
        unique.setdefault(fingerprint, candidate)
    if not unique:
        raise ObservationUnavailable(f"缺少 {binding.source}.{binding.name}")
    if len(unique) > 1:
        raise ObservationUnavailable(f"{binding.source}.{binding.name} 存在冲突值")
    return next(iter(unique.values()))


def _page_fact(
    normalized: NormalizedObservation,
    binding: ObservationBinding,
) -> _BoundFact:
    if binding.name not in normalized.page or normalized.page[binding.name] in (None, ""):
        raise ObservationUnavailable(f"缺少 page.{binding.name}")
    return _BoundFact(normalized.page[binding.name], f"page.{binding.name}")


def _field_fact(
    normalized: NormalizedObservation,
    binding: ObservationBinding,
) -> _BoundFact:
    target = _key(binding.name)
    controls: list[_BoundFact] = []
    for index, control in enumerate(normalized.controls):
        identities = {
            _key(control.get(name, ""))
            for name in ("label", "name", "id", "group_field")
        }
        if target not in identities:
            continue
        for value_name in ("selected_text_primary", "selected_text", "value"):
            if value_name in control:
                controls.append(_BoundFact(
                    json_value(control[value_name]),
                    f"controls[{index}].{value_name}",
                ))
                break
    if controls:
        return _one(controls, binding)

    semantic: list[_BoundFact] = []
    for index, item in enumerate(normalized.semantic):
        if _key(item.get("key", "")) == target:
            value_name = "value" if "value" in item else "key"
            semantic.append(_BoundFact(
                json_value(item[value_name]),
                f"semantic[{index}].{value_name}",
            ))
        elif _key(item.get("role", "")) == target and item.get("key") not in (None, ""):
            semantic.append(_BoundFact(
                json_value(item["key"]),
                f"semantic[{index}].key",
            ))
    if semantic:
        return _one(semantic, binding)

    records: list[_BoundFact] = []
    for dataset_index, dataset in enumerate(normalized.datasets):
        if len(dataset.records) != 1:
            continue
        for name, value in dataset.records[0].items():
            if _key(name) == target:
                records.append(_BoundFact(
                    value,
                    f"datasets[{dataset_index}].rows[0].{name}",
                ))
    return _one(records, binding)


def _dataset_fact(
    normalized: NormalizedObservation,
    binding: ObservationBinding,
) -> _BoundFact:
    target = _key(binding.name)
    candidates: list[_BoundFact] = []
    for index, dataset in enumerate(normalized.datasets):
        if target in {_key("rows"), _key("records")}:
            candidates.append(_BoundFact(
                [dict(record) for record in dataset.records],
                f"datasets[{index}].rows",
            ))
        elif target in {
            _key("total"),
            _key("total_records"),
            _key("record_count"),
            _key("count"),
        } and dataset.total is not None:
            candidates.append(_BoundFact(
                dataset.total,
                f"datasets[{index}].total_records",
            ))
    return _one(candidates, binding)


def _structural_fact(
    normalized: NormalizedObservation,
    binding: ObservationBinding,
) -> _BoundFact:
    if binding.source == "page":
        return _page_fact(normalized, binding)
    if binding.source == "dataset":
        return _dataset_fact(normalized, binding)
    return _field_fact(normalized, binding)


def _visual_facts(
    unresolved: dict[str, ObservationBinding],
    returns: dict[str, OutputSpec],
    observation: Observation,
    *,
    check_knowledge: str,
    prepare_vision_prompt_png,
) -> dict[str, _BoundFact]:
    from gui_agent.core.run.structured_read import structured_read

    fields = list(unresolved)
    values = structured_read(
        observation.png_bytes,
        fields,
        read_spec="\n".join(
            f"{output}: {binding.name}; {returns[output].description}".rstrip("; ")
            for output, binding in unresolved.items()
        ),
        check_knowledge=check_knowledge,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
    )
    facts: dict[str, _BoundFact] = {}
    for output in fields:
        value = values.get(output)
        if value in (None, "") and returns[output].required:
            continue
        facts[output] = _BoundFact(json_value(value), f"visual.{unresolved[output].name}")
    return facts


def execute_read(
    invocation: StatementInvocation,
    *,
    observation: Observation | None,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    say: Callable[[str], None] = lambda _message: None,
    status: Callable[[str], None] = lambda _message: None,
) -> StatementOutcome:
    statement = invocation.statement
    if not isinstance(statement, Read):
        raise TypeError("execute_read requires a Read invocation")
    unexpected_inputs = set(invocation.inputs) - {"ui_state"}
    if unexpected_inputs:
        return StatementOutcome.exhausted(
            "Read cannot consume business inputs; compute from ctx results in Python",
            observation=observation,
            failure_evidence=f"read received business inputs: {sorted(unexpected_inputs)}",
        )
    if observation is None:
        return StatementOutcome.failed(
            "Read 没有可绑定的 observation",
        )

    normalized = materialize_observation(observation)
    facts: dict[str, _BoundFact] = {}
    unresolved: dict[str, ObservationBinding] = {}
    for output, binding in statement.reads.items():
        try:
            facts[output] = _structural_fact(normalized, binding)
        except ObservationUnavailable:
            unresolved[output] = binding

    visual = {
        output: binding
        for output, binding in unresolved.items()
        if binding.source == "field" and observation.png_bytes
    }
    if visual:
        try:
            facts.update(_visual_facts(
                visual,
                statement.returns,
                observation,
                check_knowledge=check_knowledge,
                prepare_vision_prompt_png=prepare_vision_prompt_png,
            ))
        except Exception:
            pass

    missing = sorted(set(statement.returns) - set(facts))
    if missing:
        status("Read 当前观察缺少声明事实")
        return StatementOutcome.failed(
            f"Read 无法绑定 outputs：{missing}",
            observation=observation,
            failure_evidence=", ".join(missing),
        )

    outputs = normalize_table_rows([{
        output: _coerce(fact.value, statement.returns[output])
        for output, fact in facts.items()
    }])[0]
    invalid = [
        output
        for output, spec in statement.returns.items()
        if not matches_output_spec(outputs[output], spec)
    ]
    if invalid:
        return StatementOutcome.exhausted(
            f"Read outputs 类型不符合合同：{invalid}",
            observation=observation,
            failure_evidence=", ".join(invalid),
        )

    say(f"  [Read] 完成：{', '.join(outputs)}")
    status("Read 观察绑定完成")
    evidence = [f"bind:{fact.evidence}->{output}" for output, fact in facts.items()]
    unverified = any(fact.evidence.startswith("visual.") for fact in facts.values())
    return StatementOutcome.completed(
        statement.goal_text,
        verification="accepted_unverified" if unverified else "confirmed",
        outputs=outputs,
        evidence=evidence,
        observation=observation,
    )


__all__ = [
    "ObservationUnavailable",
    "execute_read",
]
