"""Pure local predicates and deterministic population selection for filter."""

import hashlib
import math
import re
from pathlib import PurePosixPath

from basket_collector.config import FilterConfig, IssuesConfig
from basket_collector.models.artifacts import (
    Changes,
    ChangeStats,
    CollectRecord,
    FilterRecord,
    Issue,
    JsonValue,
    RejectedChangeRequest,
    RejectedRecord,
    RejectionReason,
)


def _reason(code: str, message: str, observed: object, expected: object) -> RejectionReason:
    """Build a structured, deterministic PR rejection reason."""
    return RejectionReason.model_validate(
        {'code': code, 'message': message, 'observed': observed, 'expected': expected}
    )


def compact_rejection(
    record: CollectRecord,
    reason: RejectionReason,
    stats: ChangeStats | None = None,
) -> RejectedRecord:
    """Preserve only public PR identity, never diff or issue body, on rejection."""
    pr = record.change_request
    if stats is None and isinstance(record, FilterRecord):
        stats = record.changes.stats
    if stats is not None:
        observed = reason.observed
        detail: dict[str, JsonValue] = dict(observed) if isinstance(observed, dict) else {'value': observed}
        detail.setdefault('stats', stats.model_dump(mode='json'))
        reason = reason.model_copy(update={'observed': detail})
    return RejectedRecord(
        id=record.id,
        change_request=RejectedChangeRequest(
            kind=pr.kind, repository=pr.repository, number=pr.number, url=pr.url, title=pr.title, author=pr.author
        ),
        reasons=[reason],
    )


def check_local(changes: Changes, config: FilterConfig) -> RejectionReason | None:
    """Check scope, inclusive size bounds, paths, then source proportion."""
    paths = [item.path for item in changes.files]
    stats = changes.stats.model_dump(mode='json')
    scope = config.scope
    if scope.enabled:
        prefix = scope.prefix
        outside = [path for path in paths if prefix and not path.startswith(prefix + '/')]
        if outside:
            return _reason('out_of_scope', 'changed path is outside scope', outside, prefix)
        if scope.subfolders_affected is not None:
            relative = [path[len(prefix) + 1 :] if prefix else path for path in paths]
            root_files = [path for path in relative if '/' not in path]
            if root_files:
                return _reason(
                    'scope_root_file', 'changed path is directly under scope', root_files, 'each path in a subfolder'
                )
            actual = len({path.split('/', 1)[0] for path in relative})
            if actual != scope.subfolders_affected:
                return _reason(
                    'subfolders_affected', 'wrong number of first-level subfolders', actual, scope.subfolders_affected
                )
    size = config.size
    if size.enabled:
        for label, value in (
            ('chars', changes.stats.patch_chars),
            ('lines', changes.stats.changed_lines),
            ('files', changes.stats.files_count),
        ):
            lower = getattr(size, f'min_{label}')
            upper = getattr(size, f'max_{label}')
            if lower is not None and value < lower:
                return _reason(f'min_{label}', f'{label} below minimum', stats, {label: {'min': lower}})
            if upper is not None and value > upper:
                return _reason(f'max_{label}', f'{label} above maximum', stats, {label: {'max': upper}})
    files = config.files
    if files.enabled:
        for pattern in files.forbidden_path_patterns:
            matches = [path for path in paths if re.search(pattern, path)]
            if matches:
                return _reason('forbidden_path', 'changed path matches forbidden pattern', matches, pattern)
        if files.allowed_path_patterns:
            disallowed = [
                path for path in paths if not any(re.search(pattern, path) for pattern in files.allowed_path_patterns)
            ]
            if disallowed:
                return _reason(
                    'disallowed_path',
                    'changed path matches no allowed pattern',
                    disallowed,
                    files.allowed_path_patterns,
                )
        for pattern in files.required_path_patterns:
            if not any(re.search(pattern, path) for path in paths):
                return _reason('required_path_missing', 'required path pattern has no match', paths, pattern)
        extensions = set(files.source_extensions)
        source_count = sum(PurePosixPath(path).suffix.lower() in extensions for path in paths)
        proportion = source_count / len(paths) if paths else 0.0
        if files.min_source_proportion is not None and proportion < files.min_source_proportion:
            return _reason(
                'min_source_proportion', 'source proportion below minimum', proportion, files.min_source_proportion
            )
        if files.max_source_proportion is not None and proportion > files.max_source_proportion:
            return _reason(
                'max_source_proportion', 'source proportion above maximum', proportion, files.max_source_proportion
            )
    return None


def check_issues(issues: list[Issue], config: IssuesConfig) -> RejectionReason | None:
    """Apply configured linked-issue cardinality requirements."""
    if not issues and not config.allow_no_issue:
        return _reason('missing_issue', 'PR has no linked issue', 0, '>= 1')
    if len(issues) > 1 and not config.allow_multiple_issues:
        return _reason('multiple_issues', 'PR has multiple linked issues', len(issues), '<= 1')
    return None


def apply_population(
    records: list[FilterRecord], config: FilterConfig
) -> tuple[list[FilterRecord], list[RejectedRecord]]:
    """Trim percentiles over all candidates, then select in stable order."""
    rejected: dict[str, RejectedRecord] = {}
    kept: dict[str, FilterRecord] = {item.id: item for item in records}
    if len(kept) != len(records):
        raise ValueError('population contains duplicate PR identities')
    percentile = config.percentile
    if percentile.enabled:
        metric = percentile.metric
        ordered = sorted(records, key=lambda item: (getattr(item.changes.stats, metric), item.id))
        lower = math.floor(len(ordered) * percentile.lower_fraction)
        upper = math.floor(len(ordered) * percentile.upper_fraction)
        for rank, item in enumerate(ordered):
            side = 'lower' if rank < lower else 'upper' if rank >= len(ordered) - upper else None
            if side is None:
                continue
            cutoff = getattr(ordered[lower - 1 if side == 'lower' else len(ordered) - upper].changes.stats, metric)
            reason = _reason(
                f'percentile_{side}',
                f'PR falls in {side} percentile tail',
                {'metric': metric, 'value': getattr(item.changes.stats, metric), 'rank': rank},
                {'cutoff': cutoff},
            )
            rejected[item.id] = compact_rejection(item, reason)
            del kept[item.id]
    selected = [item for item in records if item.id in kept]
    if config.selection.shuffle:
        seed = config.selection.seed
        selected.sort(key=lambda item: (hashlib.sha256(f'{seed}\n{item.id}'.encode('utf-8')).hexdigest(), item.id))
    limit = config.selection.limit
    if limit is not None:
        for item in selected[limit:]:
            rejected[item.id] = compact_rejection(
                item, _reason('selection_limit', 'PR falls beyond selection limit', item.id, limit)
            )
        selected = selected[:limit]
    return selected, [rejected[item.id] for item in records if item.id in rejected]
