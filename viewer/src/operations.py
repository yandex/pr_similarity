"""Consumer validation of exact state-selected operation snapshots and results."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from viewer.errors import ViewerError
from viewer.util import digest, parse_json, safe_path


def _require(condition: object) -> None:
    """Reject provenance mismatches without exposing submitted document values."""
    if not condition:
        raise ViewerError('invalid_state', 'Uploaded operation evidence is inconsistent.')


@dataclass
class OperationStore:
    """Read immutable archive evidence through its verified member inventory."""

    registry: dict
    state: dict
    inventory: dict[str, dict]
    read_file: Callable[[str], bytes]
    validate_report: Callable[[dict], None]

    def document(self, path: str) -> dict:
        """Decode one inventory-owned JSON document with exact byte identity."""
        _require(path in self.inventory and safe_path(path) == path)
        content = self.read_file(path)
        receipt = self.inventory[path]
        _require(len(content) == receipt['size_bytes'] and digest(content) == receipt['sha256'])
        value = parse_json(content)
        _require(isinstance(value, dict))
        assert isinstance(value, dict)
        return value

    def read_artifact(self, receipt: dict) -> dict:
        """Read a selected receipt only when its fields and actual member bytes match."""
        _require(isinstance(receipt, dict) and set(receipt) == {'path', 'size_bytes', 'sha256'})
        _require(type(receipt['size_bytes']) is int and self.inventory.get(receipt['path']) == receipt)
        return self.document(receipt['path'])

    def _owner(self, value: dict, identifier: str, kind: str, family: str) -> None:
        """Bind operation metadata to its UUID and frozen experiment inputs."""
        baseline = self.registry['baseline']
        _require(
            value.get('schema_family') == 'pr_similarity_os.' + family
            and type(value.get('schema_version')) is int
            and value['schema_version'] == 1
            and value.get('id') == identifier
            and value.get('kind') == kind
            and value.get('run_id') == self.registry['id']
            and value.get('basket_sha256') == baseline['basket']['sha256']
            and value.get('config_identity_sha256') == baseline['config']['identity_sha256']
        )

    def operation(self, selection: dict, kind: str) -> dict:
        """Validate a completed result, its input snapshots and accepted commit revision."""
        _require(
            isinstance(selection, dict) and set(selection) == {'id', 'kind', 'accepted_revision', 'result', 'receipt'}
        )
        identifier = selection['id']
        try:
            _require(isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier)
        except (ValueError, AttributeError, TypeError) as error:
            raise ViewerError('invalid_state', 'Operation UUID is invalid.') from error
        _require(kind in ('rescore', 'reprice') and selection['kind'] == kind)
        prefix = f'operations/{kind}/{identifier}/'
        _require(selection['result']['path'] == prefix + 'result.json')
        _require(selection['receipt']['path'] == prefix + 'receipt.json')
        result, receipt = self.read_artifact(selection['result']), self.read_artifact(selection['receipt'])
        self._owner(result, identifier, kind, 'operation_result')
        self._owner(receipt, identifier, kind, 'operation_receipt')
        _require(receipt.get('status') in ('completed', 'completed_with_gaps'))
        _require(receipt.get('result') == selection['result'])
        _require(receipt['input_state']['path'] == prefix + 'input_state.json')
        _require(receipt['input_report']['path'] == prefix + 'input_report.json')
        inputs = self.read_artifact(receipt['input_state'])
        report = self.read_artifact(receipt['input_report'])
        self.validate_report(report)
        _require(
            inputs.get('schema_family') == 'pr_similarity_os.state'
            and type(inputs.get('schema_version')) is int
            and inputs['schema_version'] == 1
            and inputs.get('run_id') == self.registry['id']
            and inputs.get('registry_sha256') == self.inventory['run.json']['sha256']
            and type(inputs.get('revision')) is int
            and inputs['revision'] >= 1
            and type(selection['accepted_revision']) is int
            and selection['accepted_revision'] == inputs['revision'] + 1
            and selection['accepted_revision'] <= self.state['revision']
        )
        _require(
            report['run']['id'] == self.registry['id']
            and report['run']['revision'] == inputs['revision']
            and report['state'] == {'path': '_meta/state.json', 'sha256': receipt['input_state']['sha256']}
            and report['basket']['sha256'] == self.registry['baseline']['basket']['sha256']
            and report['config']['identity_sha256'] == self.registry['baseline']['config']['identity_sha256']
        )
        self._items(result, inputs, kind)
        return result

    def _items(self, result: dict, inputs: dict, kind: str) -> None:
        """Require unique task/attempt items belonging to the operation's frozen input state."""
        rows = result.get('tasks' if kind == 'rescore' else 'attempts')
        _require(isinstance(rows, list) and isinstance(inputs.get('tasks'), list))
        allowed = set()
        for task in inputs['tasks']:
            owners = [*task['history'], task] if kind == 'reprice' else [task]
            for owner in owners:
                if owner['attempt'] is not None:
                    allowed.add((task['id'], owner['attempt']['id']))
        seen = set()
        assert isinstance(rows, list)
        for row in rows:
            key = (row['task_id'], row['attempt_id'])
            _require(key in allowed and key not in seen)
            seen.add(key)

    def item(self, selection: dict, kind: str, task_id: str, attempt_id: str) -> dict:
        """Resolve one explicit state-selected task result without latest-directory discovery."""
        result = self.operation(selection, kind)
        rows = result['tasks' if kind == 'rescore' else 'attempts']
        matches = [row for row in rows if (row['task_id'], row['attempt_id']) == (task_id, attempt_id)]
        _require(len(matches) == 1)
        return matches[0]

    def price_result(self, selection: dict) -> dict:
        """Expose a verified pricing result to the shared lossless coverage projection."""
        return self.operation(selection, 'reprice')

    def last_operation(self, kind: str) -> dict | None:
        """Resolve the accepted root operation while keeping absent pricing explicitly absent."""
        selected = self.state['selected_operations'][kind]
        return None if selected is None else self.operation(selected, kind)

    def validate_usage(self, ledger: dict) -> None:
        """Validate unique billing units and acyclic native session ownership."""
        try:
            _usage_validator().validate(ledger)
        except ValidationError as error:
            raise ViewerError('invalid_usage', 'Usage ledger has an incompatible schema.') from error
        _require(type(ledger['schema_version']) is int)
        sessions = {row['id']: row for row in ledger['sessions']}
        _require(len(sessions) == len(ledger['sessions']))
        _require(sum(row['kind'] == 'root' for row in sessions.values()) <= 1)
        for row in sessions.values():
            current, visited = row['parent_id'], {row['id']}
            _require(row['kind'] not in ('root', 'detached') or current is None)
            _require(row['kind'] != 'subagent' or current is not None)
            while current is not None:
                _require(current in sessions and current not in visited)
                visited.add(current)
                current = sessions[current]['parent_id']
        identifiers = [row['id'] for row in ledger['requests']]
        _require(len(identifiers) == len(set(identifiers)))
        _require(all(row['session_id'] in sessions for row in ledger['requests']))
        _require(not ledger['complete'] or all(row['complete'] for row in ledger['requests']))


@cache
def _usage_validator() -> Draft202012Validator:
    """Load the exact vendored producer usage contract from the installed viewer wheel."""
    schema = parse_json(files('viewer').joinpath('schemas/usage-v1.json').read_bytes())
    return Draft202012Validator(schema, format_checker=FormatChecker())
