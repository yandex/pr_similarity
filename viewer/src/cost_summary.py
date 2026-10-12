"""Lossless cost coverage over current and historical state-selected native attempts."""

from decimal import Decimal, localcontext
from typing import Protocol


class CostEvidenceError(Exception):
    """Report safe contract failures independently of either caller's runtime errors."""

    def __init__(self, code: str, message: str) -> None:
        """Retain only an explicit stable code and credential-free explanation."""
        super().__init__(message)
        self.code = code
        self.message = message


class CostStore(Protocol):
    """Expose authoritative selections without importing the snapshot implementation."""

    registry: dict
    state: dict

    def read_artifact(self, receipt: dict) -> dict:
        """Read an exact immutable artifact after adapter-owned size, digest and path validation."""
        ...

    def price_result(self, selection: dict) -> dict:
        """Return the exact reprice result after adapter-owned operation provenance validation."""
        ...

    def validate_usage(self, ledger: dict) -> None:
        """Prove the caller's usage schema, request uniqueness and session ownership."""
        ...

    def last_operation(self, kind: str) -> dict | None:
        """Return the exact accepted root operation for the requested result kind."""
        ...


def _sum(values: list[Decimal]) -> Decimal:
    """Sum stored monetary values without the ambient Decimal context rounding them."""
    with localcontext() as context:
        context.prec = max(
            64,
            max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in values), default=1)
            + len(str(len(values)))
            + 8,
        )
        return sum(values, Decimal(0))


def _unknown(*, started: bool | None = None, total_requests: int | None = None) -> dict:
    """Represent unavailable measurement with explicit coverage instead of a synthetic zero."""
    return {
        'subtotal': Decimal(0),
        'complete': False,
        'known': False,
        'known_requests': 0,
        'total_requests': total_requests,
        'started': started,
    }


def _native(store: CostStore, owner: dict) -> tuple[dict, dict | None, dict | None]:
    """Read request-level reported cost only from a signed, attempt-owned usage ledger."""
    if owner['seal'] is None:
        return _unknown(), None, None
    native = store.read_artifact(owner['seal'])
    row = native['task']
    started = row['execution']['status'] != 'not_started'
    timing = native.get('native_timing')
    if isinstance(timing, dict):
        started = timing['started_at'] is not None
    ledger_ref = row['telemetry']['usage_ledger']
    if ledger_ref is None:
        return _unknown(started=started), None, None
    owned = {item['path']: item for item in native['artifact_receipts']}
    receipt = owned.get(ledger_ref['path'])
    if receipt is None:
        return _unknown(started=started), None, None
    if receipt['sha256'] != ledger_ref['sha256']:
        raise CostEvidenceError('usage_digest_mismatch', 'Declared cost ledger differs from its native seal receipt.')
    ledger = store.read_artifact(receipt)
    store.validate_usage(ledger)
    if (ledger['run_id'], ledger['task_id'], ledger['attempt_id']) != (
        store.registry['id'],
        owner['task_id'],
        owner['attempt']['id'],
    ):
        raise CostEvidenceError('usage_owner_mismatch', 'Cost summary ledger belongs to a different native attempt.')
    requests = ledger['requests']
    known = [
        Decimal(str(item['reported_cost_usd']))
        for item in requests
        if item['complete'] and item['reported_cost_usd'] is not None
    ]
    complete = ledger['complete'] and len(known) == len(requests)
    value = {
        'subtotal': _sum(known),
        'complete': complete,
        'known': bool(known) or complete,
        'known_requests': len(known),
        'total_requests': len(requests),
        'started': started,
    }
    return value, ledger, receipt


def _priced(store: CostStore, owner: dict, ledger: dict | None, ledger_ref: dict | None) -> dict:
    """Use this attempt's selected price rather than copying a previous root receipt total."""
    total = None if ledger is None else len(ledger['requests'])
    selection = owner['pricing']
    if selection is None:
        return _unknown(total_requests=total)
    result_document = store.price_result(selection)
    matches = [
        item
        for item in result_document['attempts']
        if (item['task_id'], item['attempt_id']) == (owner['task_id'], owner['attempt']['id'])
    ]
    if len(matches) != 1:
        raise CostEvidenceError('operation_owner_mismatch', 'Selected cost has no unique registered attempt result.')
    item = matches[0]
    source = item['inputs']['usage']
    if source is not None and (ledger_ref is None or source != ledger_ref):
        raise CostEvidenceError('usage_digest_mismatch', 'Selected price differs from current sealed usage.')
    result = item['result']
    complete = result['status'] == 'complete'
    subtotal = Decimal(result['known_subtotal'])
    if source is None and (complete or result['priced_requests'] > 0 or subtotal > 0):
        raise CostEvidenceError('usage_receipt_missing', 'Known selected pricing requires its sealed usage input.')
    if not subtotal.is_finite() or subtotal < 0:
        raise CostEvidenceError('invalid_pricing', 'Selected cost subtotal must be finite and nonnegative.')
    return {
        'subtotal': subtotal,
        'complete': complete,
        'known': result['priced_requests'] > 0 or complete,
        'known_requests': result['priced_requests'],
        'total_requests': total if total is not None else result['total_requests'],
        'started': None,
    }


def _summary(values: list[dict], missing: int, *, requested: bool = True, native: bool = False) -> dict:
    """Keep complete amounts, known subtotals and attempt/request coverage independent."""
    complete = sum(value['complete'] for value in values)
    known = sum(value['known'] for value in values)
    full = bool(values) and complete == len(values) and not missing
    subtotal = _sum([value['subtotal'] for value in values])
    totals = [value['total_requests'] for value in values]
    if not requested:
        status = 'not_requested'
    elif not values or (native and not known and all(value['started'] is False for value in values)):
        status = 'not_started'
    elif full:
        status = 'complete'
    elif known:
        status = 'partial'
    else:
        status = 'unavailable'
    return {
        'status': status,
        'currency': 'USD',
        'amount': format(subtotal, 'f') if full and requested else None,
        'known_subtotal': format(subtotal, 'f'),
        'coverage': {
            'total_attempts': len(values),
            'complete_attempts': complete,
            'known_attempts': known,
            'known_requests': sum(value['known_requests'] for value in values),
            'total_requests': None if any(total is None for total in totals) else sum(totals),
            'missing_tasks': missing,
        },
    }


def build_cost_summary(store: CostStore) -> dict:
    """Project native and API-equivalent cost from exact current/history selections.

    Args:
        store: Validated authoritative registry, state and immutable artifact references.

    Returns:
        Final-attempt and all-attempt summaries with lossless monetary strings,
        nullable complete amounts, coverage and explicit not-requested pricing.

    Raises:
        CostEvidenceError: If selected billing inputs have inconsistent evidence ownership.

    """
    scopes: dict[str, dict[str, list[dict]]] = {
        'final_attempts': {'native_reported': [], 'api_equivalent': []},
        'all_attempts': {'native_reported': [], 'api_equivalent': []},
    }
    requested = store.last_operation('reprice') is not None
    for task in store.state['tasks']:
        owners = [*task['history'], task] if task['attempt'] is not None else task['history']
        for selected in owners:
            owner = {**selected, 'task_id': task['id']}
            reported, ledger, receipt = _native(store, owner)
            priced = _priced(store, owner, ledger, receipt)
            requested |= owner['pricing'] is not None
            for name, value in (('native_reported', reported), ('api_equivalent', priced)):
                scopes['all_attempts'][name].append(value)
                if selected is task:
                    scopes['final_attempts'][name].append(value)
    result = {}
    for scope, values in scopes.items():
        missing = sum(task['attempt'] is None for task in store.state['tasks']) if scope == 'final_attempts' else 0
        result[scope] = {
            'native_reported': _summary(values['native_reported'], missing, native=True),
            'api_equivalent': _summary(values['api_equivalent'], missing, requested=requested),
        }
    return result
