"""Normalize GitHub API reads into the shared RemoteVCS contract."""

from collections.abc import Iterator
from datetime import datetime
from threading import Event

from basket_collector.config import GitHubConfig, SourceConfig
from basket_collector.contracts import (
    ChangeRequest,
    CollectRecord,
    Issue,
    Revisions,
    SourceRevisions,
)
from basket_collector.github import (
    GitHubClient,
    GitHubError,
    _object,
    _positive_int,
    _string,
    github_url,
)
from basket_collector.remote import Candidate
from basket_collector.sources import Repository, SourceDescriptor, change_id


def merge_time(value: object) -> str:
    """Normalize an available merge instant to canonical UTC precision."""
    if not isinstance(value, str) or not value.endswith('Z'):
        raise GitHubError('Source returned an invalid merge timestamp.', code='invalid_source_metadata')
    try:
        return (
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            .isoformat(timespec='microseconds')
            .replace('+00:00', 'Z')
        )
    except ValueError as exc:
        raise GitHubError('Source returned an invalid merge timestamp.', code='invalid_source_metadata') from exc


class GitHubRemoteVCS:
    """Keep GitHub-specific URLs, fields and refs out of shared stages."""

    def __init__(
        self, config: SourceConfig, *, client: GitHubClient | None = None, stop_event: Event | None = None
    ) -> None:
        """Configure the native reader without opening network connections."""
        native = GitHubConfig.model_validate(config.model_dump(exclude={'provider', 'project', 'server_url'}))
        self.client = client or GitHubClient(native, stop_event=stop_event)
        self.owns_client = client is None

    def close(self) -> None:
        """Close the native HTTP client only when this adapter created it."""
        if self.owns_client:
            self.client.close()

    def resolve_repository(self, locator: str) -> Repository:
        """Return the stable GitHub project ID with canonical display metadata."""
        return self.client.repository(locator)

    def list_merged_candidates(
        self,
        repository: Repository,
        *,
        start_page: int = 1,
        merged_from: str | None = None,
        merged_before: str | None = None,
    ) -> Iterator[tuple[int, list[Candidate]]]:
        """Discard unmerged closed PRs before exposing normalized discovery facts."""
        for page, rows in self.client.list_closed_prs(repository.path, start_page=start_page):
            candidates = []
            for row in rows:
                if row.get('merged_at') is None:
                    continue
                base = _object(row.get('base'), 'PR base')
                candidates.append(
                    Candidate(
                        _positive_int(row.get('number'), 'PR number'),
                        merge_time(row['merged_at']),
                        _string(base.get('ref'), 'target branch'),
                    )
                )
            yield page, candidates

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Freeze the original source head, not a synthetic integration commit."""
        raw = self.client.get_pull_request(repository.path, number)
        if _positive_int(raw.get('number'), 'PR number') != number or raw.get('merged_at') is None:
            raise GitHubError('Selected change is not the requested merged PR.', code='invalid_source_metadata')
        base = _object(raw.get('base'), 'PR base')
        head = _object(raw.get('head'), 'PR head')
        base_repo = _object(base.get('repo'), 'PR target project')
        if (
            _string(base_repo.get('full_name'), 'target path').casefold() != repository.path.casefold()
            or str(_positive_int(base_repo.get('id'), 'target project ID')) != repository.project_id
        ):
            raise GitHubError('Selected PR belongs to another target project.', code='invalid_source_metadata')
        head_repo = head.get('repo')
        user = raw.get('user')
        body = raw.get('body') or ''
        if not isinstance(body, str):
            raise GitHubError('Source returned an invalid PR body.', code='invalid_source_metadata')
        request = ChangeRequest(
            kind='pull_request',
            repository=repository.path,
            number=number,
            url=github_url(raw.get('html_url'), 'PR URL'),
            title=_string(raw.get('title'), 'PR title'),
            body=body,
            author=None if user is None else _string(_object(user, 'author').get('login'), 'author login'),
            merged_at=merge_time(raw['merged_at']),
        )
        source = SourceDescriptor(
            vcs='git',
            fetch_url=repository.web_url + '.git',
            fetch_refs=[f'refs/pull/{number}/head', 'refs/heads/' + _string(base.get('ref'), 'target branch')],
        )
        evidence = SourceRevisions(
            base_sha=_string(base.get('sha'), 'target SHA'),
            head_sha=_string(head.get('sha'), 'source SHA'),
            merge_commit_sha=raw.get('merge_commit_sha'),
            squash_commit_sha=None,
            expected_base_sha=None,
            diff_version_id=None,
            head_repository=(
                None
                if head_repo is None
                else _string(_object(head_repo, 'source project').get('full_name'), 'source path')
            ),
            commits_count=_positive_int(raw.get('commits'), 'commit count'),
            target_branch=_string(base.get('ref'), 'target branch'),
        )
        return CollectRecord(
            id=change_id(repository.provider, repository.server_url, repository.project_id, request.kind, number),
            change_request=request,
            revisions=Revisions(source=evidence),
            source=source,
        )

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Expose the native commit list as exact SHA data only."""
        return [
            _string(item.get('sha'), 'commit SHA')
            for item in self.client.get_pull_request_commits(
                record.change_request.repository, record.change_request.number
            )
        ]

    def get_closing_issues(self, record: CollectRecord) -> list[Issue]:
        """Use explicit closing references, including accessible private issues."""
        return self.client.get_closing_issues(record.change_request.repository, record.change_request.number)
