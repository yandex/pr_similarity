"""Safe typed inference facts from explicitly selected native options."""

from pathlib import PurePosixPath

from benchmark.models.evidence import ConfigIdentity, EffectiveConfig
from benchmark.models.report import (
    ReportInference,
    ReportInferenceConfigured,
    ReportInferenceEffective,
    ReportInferenceEffectiveAttemptItem,
    ReportInferenceValue,
)

from .native_inference import extract, fact, identifier


def _source_ref(value: object) -> str:
    """Keep the actual relative evidence reference without accepting host paths."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError('Invalid inference evidence reference.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')) or '\\' in value:
        raise ValueError('Invalid inference evidence reference.')
    return value


def _consensus(values: list[ReportInferenceValue]) -> ReportInferenceValue:
    """Aggregate one control while preserving each original attempt source separately."""
    signatures = {(value.status, value.value, value.reason) for value in values}
    if not signatures:
        return fact('unknown', None, None, '', 'effective_evidence_unavailable')
    if len(signatures) > 1:
        return fact('mixed', None, None, '', 'effective_attempts_disagree')
    result = values[0].model_copy(deep=True)
    if len(values) > 1:
        result.source = None
    return result


def build_inference(
    identity: ConfigIdentity, effective_configs: list[EffectiveConfig] | None = None
) -> ReportInference:
    """Extract configured facts and consensus over every supplied native attempt."""
    configured = extract(identity.native_config, 'config_identity.json', 'native_config.')
    attempts: list[ReportInferenceEffectiveAttemptItem] = []
    for item in effective_configs or []:
        if not identifier(item.attempt_id):
            raise ValueError('Invalid inference attempt identity.')
        ref = _source_ref(item.source_ref)
        values = extract(item.config, ref)
        attempts.append(
            ReportInferenceEffectiveAttemptItem(
                attempt_id=item.attempt_id,
                source_ref=ref,
                provider=values.provider,
                model=values.model,
                effort=values.effort,
            )
        )
    effective = ReportInferenceEffective(
        provider=_consensus([row.provider for row in attempts]),
        model=_consensus([row.model for row in attempts]),
        effort=_consensus([row.effort for row in attempts]),
        attempts=attempts,
    )
    pairs = (
        (configured.provider, effective.provider),
        (configured.model, effective.model),
        (configured.effort, effective.effort),
    )
    discrepancy = any(
        value.status == 'mixed'
        or (value.status == 'known' and (expected.status, expected.value) != ('known', value.value))
        for expected, value in pairs
    )
    return ReportInference(schema_version=1, configured=configured, effective=effective, discrepancy=discrepancy)


def generated_groups(inference: ReportInference) -> dict[str, list[str]]:
    """Generate stable groups only from known frozen configured inference."""
    configured: ReportInferenceConfigured = inference.configured
    model, effort = configured.model, configured.effort
    label = model.value if model.status == 'known' else None
    result: dict[str, list[str]] = {'model': []}
    if effort.status == 'known':
        assert effort.value is not None
        result['effort'] = [effort.value]
        if label is not None:
            label += ' [' + effort.value + ']'
    if label is not None:
        result['model'] = [label]
    return result
