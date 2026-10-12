"""Freeze deterministic queues and normalized change snapshots independently of hosting."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from basket_collector.config import CollectConfig
from basket_collector.contracts import CollectRecord, Discovery
from basket_collector.remote import RemoteError, RemoteVCS
from basket_collector.sources import Repository


@dataclass(frozen=True)
class DiscoveryResult:
    """Retain the selected queue and complete observed merge ordering."""

    numbers: tuple[int, ...]
    discovery: Discovery
    matches: tuple[tuple[str, int], ...]


def select_change_ids(config: CollectConfig) -> DiscoveryResult:
    """Preserve explicit operator order before any external operation."""
    if config.selection != 'change_ids' or config.change_ids is None:
        raise ValueError('select_change_ids requires collect.selection=change_ids')
    numbers = tuple(config.change_ids)
    return DiscoveryResult(numbers, Discovery(matched=len(numbers), selected=len(numbers), capped=False), ())


def discover_date_range(
    client: RemoteVCS,
    repository: Repository,
    config: CollectConfig,
    *,
    start_page: int = 1,
    previous_matches: tuple[tuple[str, int], ...] = (),
    on_page: Callable[[int, tuple[tuple[str, int], ...]], None] | None = None,
) -> DiscoveryResult:
    """Exhaust candidate pages, enforce the exact interval and freeze the deterministic cap."""
    if config.selection != 'date_range' or config.merged_from is None or config.merged_before is None:
        raise ValueError('discover_date_range requires both merge-date boundaries')
    if start_page < 1:
        raise ValueError('start_page must be positive')
    start = datetime.fromisoformat(config.merged_from.replace('Z', '+00:00'))
    end = datetime.fromisoformat(config.merged_before.replace('Z', '+00:00'))
    observed = {}
    for stamp, number in previous_matches:
        if (
            type(number) is not int
            or number <= 0
            or not start <= datetime.fromisoformat(stamp.replace('Z', '+00:00')) < end
        ):
            raise ValueError('checkpoint match falls outside the selected population')
        observed[number] = stamp
    for page, candidates in client.list_merged_candidates(
        repository, start_page=start_page, merged_from=config.merged_from, merged_before=config.merged_before
    ):
        for candidate in candidates:
            instant = datetime.fromisoformat(candidate.merged_at.replace('Z', '+00:00'))
            if start <= instant < end and (
                not config.default_branch_only or candidate.target_branch == repository.default_branch
            ):
                observed.setdefault(candidate.number, candidate.merged_at)
        if on_page is not None:
            on_page(page + 1, tuple(sorted((stamp, number) for number, stamp in observed.items())))
    matches = tuple(sorted((stamp, number) for number, stamp in observed.items()))
    selected = matches[: config.max_changes]
    return DiscoveryResult(
        tuple(number for _, number in selected),
        Discovery(matched=len(matches), selected=len(selected), capped=len(selected) < len(matches)),
        matches,
    )


def collect_change(client: RemoteVCS, repository: Repository, number: int, config: CollectConfig) -> CollectRecord:
    """Validate provider-normalized merged metadata against the selected target."""
    record = client.get_change_request(repository, number)
    if config.default_branch_only and record.revisions.source.target_branch != repository.default_branch:
        raise RemoteError(
            'Selected change targets another branch; check collect.default_branch_only.', code='non_default_branch'
        )
    return record
