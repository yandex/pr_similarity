"""Consumer validation of exact state-selected operation snapshots and results."""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from viewer.errors import ViewerError
from viewer.models import Report, Run, State, Usage, WireModel
from viewer.models.evidence import (
    OperationInputState,
    OperationOwner,
    OperationReceipt,
    OperationResult,
    PriceItem,
    Receipt,
    ScoreItem,
    parse_evidence,
)
from viewer.models.evidence import OperationSelection as Selection
from viewer.util import digest, parse_json, require_object, safe_path


def _require(condition: object) -> None:
    """Reject provenance mismatches without exposing submitted document values."""
    if not condition:
        raise ViewerError('invalid_state', 'Uploaded operation evidence is inconsistent.')


@dataclass
class OperationStore:
    """Read immutable archive evidence through its verified member inventory."""

    registry: Run
    state: State
    inventory: Mapping[str, Receipt]
    read_file: Callable[[str], bytes]
    validate_report: Callable[[object], Report]

    def document[T: WireModel](self, path: str, model: type[T]) -> T:
        """Decode one typed inventory-owned document with exact byte identity."""
        _require(path in self.inventory and safe_path(path) == path)
        content = self.read_file(path)
        receipt = self.inventory[path]
        _require(len(content) == receipt.size_bytes and digest(content) == receipt.sha256)
        value = parse_json(content)
        if model is Report:
            self.validate_report(value)
        if model is Usage:
            try:
                _usage_validator().validate(value)
            except ValidationError as error:
                raise ViewerError('invalid_usage', 'Usage ledger has an incompatible schema.') from error
        return parse_evidence(model, value, message='Uploaded operation evidence is inconsistent.')

    def read_artifact[T: WireModel](self, receipt: Receipt, model: type[T]) -> T:
        """Read a selected receipt only when its fields and actual member bytes match."""
        _require(set(receipt.to_wire()) == {'path', 'size_bytes', 'sha256'})
        actual = self.inventory.get(receipt.path)
        _require(type(receipt.size_bytes) is int and actual is not None)
        assert actual is not None
        _require(actual.to_wire() == receipt.to_wire())
        return self.document(receipt.path, model)

    def _owner(self, value: OperationOwner, identifier: str, kind: str) -> None:
        """Bind operation metadata to its UUID and frozen experiment inputs."""
        baseline = self.registry.baseline
        _require(
            type(value.schema_version) is int
            and value.schema_version == self.registry.schema_version
            and value.id == identifier
            and value.kind == kind
            and value.run_id == self.registry.id
            and value.basket_sha256 == baseline.basket.sha256
            and value.config_identity_sha256 == baseline.config.identity_sha256
        )

    def operation(self, selection: Selection, kind: str) -> OperationResult:
        """Validate a completed result, its input snapshots and accepted commit revision."""
        _require(set(selection.to_wire()) == {'id', 'kind', 'accepted_revision', 'result', 'receipt'})
        identifier = selection.id
        try:
            _require(str(uuid.UUID(identifier)) == identifier)
        except (ValueError, AttributeError, TypeError) as error:
            raise ViewerError('invalid_state', 'Operation UUID is invalid.') from error
        _require(kind in ('rescore', 'reprice') and selection.kind == kind)
        prefix = f'operations/{kind}/{identifier}/'
        _require(selection.result.path == prefix + 'result.json')
        _require(selection.receipt.path == prefix + 'receipt.json')
        result = self.read_artifact(selection.result, OperationResult)
        receipt = self.read_artifact(selection.receipt, OperationReceipt)
        self._owner(result, identifier, kind)
        self._owner(receipt, identifier, kind)
        _require(receipt.status in ('completed', 'completed_with_gaps'))
        _require(receipt.result.to_wire() == selection.result.to_wire())
        _require(receipt.input_state.path == prefix + 'input_state.json')
        _require(receipt.input_report.path == prefix + 'input_report.json')
        inputs = self.read_artifact(receipt.input_state, OperationInputState)
        report = self.read_artifact(receipt.input_report, Report)
        _require(
            type(inputs.schema_version) is int
            and inputs.schema_version == self.registry.schema_version
            and inputs.run_id == self.registry.id
            and inputs.registry_sha256 == self.inventory['run.json'].sha256
            and type(inputs.revision) is int
            and inputs.revision >= 1
            and type(selection.accepted_revision) is int
            and selection.accepted_revision == inputs.revision + 1
            and selection.accepted_revision <= self.state.revision
        )
        _require(
            report.schema_version == self.registry.schema_version
            and report.run.id == self.registry.id
            and report.run.revision == inputs.revision
            and report.state.to_wire() == {'path': '_meta/state.json', 'sha256': receipt.input_state.sha256}
            and report.basket.sha256 == self.registry.baseline.basket.sha256
            and report.config.identity_sha256 == self.registry.baseline.config.identity_sha256
        )
        self._items(result, inputs, kind)
        return result

    def _items(self, result: OperationResult, inputs: OperationInputState, kind: str) -> None:
        """Require unique task/attempt items belonging to the operation's frozen input state."""
        rows = result.tasks if kind == 'rescore' else result.attempts
        _require(rows is not None)
        assert rows is not None
        allowed = set()
        for task in inputs.tasks:
            owners = [*task.history, task] if kind == 'reprice' else [task]
            for owner in owners:
                if owner.attempt is not None:
                    allowed.add((task.id, owner.attempt.id))
        seen = set()
        for row in rows:
            key = (row.task_id, row.attempt_id)
            _require(key in allowed and key not in seen)
            seen.add(key)

    def item(self, selection: Selection, kind: str, task_id: str, attempt_id: str) -> ScoreItem | PriceItem:
        """Resolve one explicit state-selected result without directory discovery."""
        result = self.operation(selection, kind)
        rows = result.tasks if kind == 'rescore' else result.attempts
        _require(rows is not None)
        assert rows is not None
        matches = [row for row in rows if (row.task_id, row.attempt_id) == (task_id, attempt_id)]
        _require(len(matches) == 1)
        return matches[0]

    def score_item(self, selection: Selection, task_id: str, attempt_id: str) -> ScoreItem:
        """Resolve an explicitly selected task score."""
        item = self.item(selection, 'rescore', task_id, attempt_id)
        _require(isinstance(item, ScoreItem))
        assert isinstance(item, ScoreItem)
        return item

    def price_item(self, selection: Selection, task_id: str, attempt_id: str) -> PriceItem:
        """Resolve an explicitly selected attempt price."""
        item = self.item(selection, 'reprice', task_id, attempt_id)
        _require(isinstance(item, PriceItem))
        assert isinstance(item, PriceItem)
        return item

    def price_result(self, selection: Selection) -> OperationResult:
        """Expose a verified pricing result to the lossless coverage projection."""
        return self.operation(selection, 'reprice')

    def last_operation(self, kind: str) -> OperationResult | None:
        """Resolve the accepted root operation while preserving absent pricing."""
        selected = (
            self.state.selected_operations.rescore if kind == 'rescore' else self.state.selected_operations.reprice
        )
        return None if selected is None else self.operation(selected, kind)

    def validate_usage(self, ledger: Usage) -> None:
        """Validate unique billing units and acyclic native session ownership."""
        try:
            _usage_validator().validate(ledger.to_wire())
        except ValidationError as error:
            raise ViewerError('invalid_usage', 'Usage ledger has an incompatible schema.') from error
        _require(type(ledger.schema_version) is int)
        sessions = {row.id: row for row in ledger.sessions}
        _require(len(sessions) == len(ledger.sessions))
        _require(sum(row.kind == 'root' for row in sessions.values()) <= 1)
        for row in sessions.values():
            current, visited = row.parent_id, {row.id}
            _require(row.kind not in ('root', 'detached') or current is None)
            _require(row.kind != 'subagent' or current is not None)
            while current is not None:
                _require(current in sessions and current not in visited)
                visited.add(current)
                current = sessions[current].parent_id
        identifiers = [row.id for row in ledger.requests]
        _require(len(identifiers) == len(set(identifiers)))
        _require(all(row.session_id in sessions for row in ledger.requests))
        _require(not ledger.complete or all(row.complete for row in ledger.requests))


@cache
def _usage_validator() -> Draft202012Validator:
    """Load the exact vendored producer usage contract from the installed viewer wheel."""
    schema = require_object(parse_json(files('viewer').joinpath('schemas/usage-v1.json').read_bytes()))
    return Draft202012Validator(schema, format_checker=FormatChecker())
