"""Safe configured and runtime inference facts from explicitly selected native options."""

from pathlib import PurePosixPath

from viewer.util import safe_path

from .native_inference import extract, fact, identifier

_FIELDS = ('provider', 'model', 'effort')


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


def build_inference(identity: dict, effective_configs: list[dict] | None = None) -> dict:
    """Extract frozen configured facts and consensus over supplied attempt evidence.

    Effective inputs contain attempt_id, source_ref and config; raw config is never
    retained. Consensus uses all attempts, including unknown or unsupported values.
    """
    configured = extract(identity.get('native_config'), 'config_identity.json', 'native_config.')
    attempts = []
    for item in effective_configs or []:
        attempt_id = item['attempt_id']
        if not identifier(attempt_id):
            raise ValueError('Invalid inference attempt identity.')
        ref = _source_ref(item['source_ref'])
        attempts.append({'attempt_id': attempt_id, 'source_ref': ref, **extract(item.get('config'), ref)})
    effective: dict = {'attempts': attempts}
    for field in _FIELDS:
        signatures = {(row[field]['status'], row[field]['value'], row[field]['reason']) for row in attempts}
        if not signatures:
            effective[field] = fact('unknown', None, None, '', 'effective_evidence_unavailable')
        elif len(signatures) > 1:
            effective[field] = fact('mixed', None, None, '', 'effective_attempts_disagree')
        else:
            effective[field] = dict(attempts[0][field])
            if len(attempts) > 1:
                effective[field]['source'] = None  # Exact origins remain in each attempt.
    discrepancy = any(
        effective[field]['status'] == 'mixed'
        or (
            effective[field]['status'] == 'known'
            and (configured[field]['status'], configured[field]['value']) != ('known', effective[field]['value'])
        )
        for field in _FIELDS
    )
    return {'schema_version': 1, 'configured': configured, 'effective': effective, 'discrepancy': discrepancy}


def generated_groups(inference: dict) -> dict[str, list[str]]:
    """Generate stable groups only from known frozen configured inference."""
    configured = inference['configured']
    model, effort = configured['model'], configured['effort']
    label = model['value'] if model['status'] == 'known' else None
    result = {'model': []}
    if effort['status'] == 'known':
        result['effort'] = [effort['value']]
        if label is not None:
            label += ' [' + effort['value'] + ']'
    if label is not None:
        result['model'] = [label]
    return result


def public_inference(value: dict) -> dict:
    """Copy validated scalar facts and exact evidence origins only."""

    def text(raw: object, maximum: int, nullable: bool = False) -> str | None:
        """Accept bounded public scalar strings, optionally absent."""
        if nullable and raw is None:
            return None
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        ):
            raise ValueError('Unsupported inference field.')
        return raw

    def fact(raw: object) -> dict:
        """Copy one validated inference value and its safe origin."""
        if not isinstance(raw, dict) or raw.get('status') not in ('known', 'unknown', 'unsupported', 'mixed'):
            raise ValueError('Unsupported inference fact.')
        status = raw['status']
        actual = text(raw.get('value'), 256, nullable=True)
        if (status == 'known') != (actual is not None):
            raise ValueError('Inconsistent inference fact.')
        source = raw.get('source')
        if source is not None:
            if not isinstance(source, dict):
                raise ValueError('Unsupported inference origin.')
            ref = text(source.get('ref'), 4096)
            assert isinstance(ref, str)
            safe_path(ref)
            source = {'ref': ref, 'field': text(source.get('field'), 1024)}
        return {
            'status': status,
            'value': actual,
            'reason': text(raw.get('reason'), 256, nullable=True),
            'source': source,
        }

    def fields(raw: object) -> dict:
        """Require all inference fields in each evidence record."""
        if not isinstance(raw, dict):
            raise ValueError('Unsupported inference record.')
        return {name: fact(raw.get(name)) for name in _FIELDS}

    if (
        not isinstance(value, dict)
        or type(value.get('schema_version')) is not int
        or value['schema_version'] != 1
        or type(value.get('discrepancy')) is not bool
    ):
        raise ValueError('Unsupported inference contract.')
    configured = fields(value.get('configured'))
    effective: dict = fields(value.get('effective'))
    attempts = value['effective'].get('attempts')
    if not isinstance(attempts, list) or len(attempts) > 100000:
        raise ValueError('Unsupported inference attempts.')
    effective['attempts'] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError('Unsupported inference attempt.')
        ref = text(attempt.get('source_ref'), 4096)
        assert isinstance(ref, str)
        safe_path(ref)
        effective['attempts'].append(
            {'attempt_id': text(attempt.get('attempt_id'), 256), 'source_ref': ref, **fields(attempt)}
        )
    return {'schema_version': 1, 'configured': configured, 'effective': effective, 'discrepancy': value['discrepancy']}
