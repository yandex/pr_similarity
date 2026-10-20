"""Normalize GitLab project and merge-request facts into the collector RemoteVCS contract."""

from collections.abc import Iterator
from threading import Event
from urllib.parse import quote

from basket_collector.config import SourceConfig
from basket_collector.contracts import (
    ChangeRequest,
    CollectRecord,
    Issue,
    RejectedChangeRequest,
    RejectedRecord,
    RejectionReason,
    Revisions,
    SourceRevisions,
)
from basket_collector.gitlab import GitLabClient
from basket_collector.remote import Candidate, ChangeRejected, RemoteError, utc_time
from basket_collector.sources import (
    Repository,
    SourceDescriptor,
    change_id,
    https_url,
    issue_id,
)


def object_field(value: object, label: str) -> dict:
    """Require a complete object without leaking its payload in an error."""
    if not isinstance(value, dict):
        raise RemoteError('GitLab returned invalid ' + label + '.', code='invalid_source_metadata')
    return value


def text_field(value: object, label: str) -> str:
    """Require nonempty textual metadata rather than coerced API values."""
    if not isinstance(value, str) or not value:
        raise RemoteError('GitLab returned invalid ' + label + '.', code='invalid_source_metadata')
    return value


def positive_id(value: object, label: str) -> int:
    """Keep global IDs and project-local iids distinct and strictly positive."""
    if type(value) is not int or value <= 0:
        raise RemoteError('GitLab returned invalid ' + label + '.', code='invalid_source_metadata')
    return value


class GitLabRemoteVCS:
    """Provide stable normalized project/MR data for GitLab.com and self-hosted API v4."""

    def __init__(
        self, config: SourceConfig, *, client: GitLabClient | None = None, stop_event: Event | None = None
    ) -> None:
        """Configure one instance without reading repository visibility or issuing a request."""
        self.config = config
        self.client = client or GitLabClient(config, stop_event=stop_event)
        self.owns_client = client is None
        self.projects: dict[str, Repository] = {}
        self.fetch_urls: dict[str, str] = {}

    def close(self) -> None:
        """Close only the native client owned by this adapter."""
        if self.owns_client:
            self.client.close()

    def resolve_repository(self, locator: str) -> Repository:
        """Resolve numeric project identity and subgroup path while preserving display case."""
        raw, _headers = self.client.request('projects/' + quote(locator, safe=''))
        project = object_field(raw, 'project')
        identity = str(positive_id(project.get('id'), 'project ID'))
        repository = Repository(
            provider='gitlab',
            server_url=self.config.server_url,
            project_id=identity,
            path=text_field(project.get('path_with_namespace'), 'project path'),
            web_url=https_url(text_field(project.get('web_url'), 'project URL')),
            default_branch=text_field(project.get('default_branch'), 'default branch'),
        )
        if locator.isascii() and locator.isdecimal() and locator != identity:
            raise RemoteError('GitLab returned another project ID.', code='source_identity_changed')
        self.projects[identity] = repository
        self.projects[repository.path] = repository
        self.fetch_urls[identity] = https_url(text_field(project.get('http_url_to_repo'), 'Git fetch URL'))
        return repository

    def list_merged_candidates(
        self,
        repository: Repository,
        *,
        start_page: int = 1,
        merged_from: str | None = None,
        merged_before: str | None = None,
    ) -> Iterator[tuple[int, list[Candidate]]]:
        """Use merged state and normalize selection facts without relying on new sort features."""
        path = 'projects/' + repository.project_id + '/merge_requests'
        params: dict[str, str | int] = {'state': 'merged', 'scope': 'all', 'order_by': 'created_at', 'sort': 'asc'}
        if merged_from is not None:
            params['merged_after'] = merged_from
        if merged_before is not None:
            params['merged_before'] = merged_before

        def discovery_pages() -> Iterator[tuple[int, list[dict]]]:
            """Fall back once if this server does not accept optional merge-date filters."""
            yielded = False
            try:
                for page in self.client.pages(path, params, start_page=start_page):
                    yielded = True
                    yield page
            except RemoteError as exc:
                if yielded or exc.code != 'source_bad_request' or not (merged_from or merged_before):
                    raise
                unfiltered = {
                    key: value for key, value in params.items() if key not in ('merged_after', 'merged_before')
                }
                yield from self.client.pages(path, unfiltered, start_page=start_page)

        for page, rows in discovery_pages():
            candidates = []
            for row in rows:
                if row.get('state') != 'merged' or row.get('merged_at') is None:
                    raise RemoteError(
                        'GitLab merged discovery returned a nonmerged change.', code='invalid_source_metadata'
                    )
                candidates.append(
                    Candidate(
                        positive_id(row.get('iid'), 'MR iid'),
                        utc_time(row.get('merged_at')),
                        text_field(row.get('target_branch'), 'target branch'),
                    )
                )
            yield page, candidates

    def _commits(self, repository_id: str, number: int) -> list[str]:
        """Exhaust commit pages instead of trusting changes_count or list summary fields."""
        path = 'projects/' + repository_id + '/merge_requests/' + str(number) + '/commits'
        return [text_field(item.get('id'), 'commit SHA') for _page, rows in self.client.pages(path) for item in rows]

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Freeze diff_refs evidence without replacing head by merge/squash SHA."""
        path = 'projects/' + repository.project_id + '/merge_requests/' + str(number)
        raw, _headers = self.client.request(path)
        request = object_field(raw, 'merge request')
        if (
            positive_id(request.get('iid'), 'MR iid') != number
            or str(positive_id(request.get('target_project_id'), 'target project ID')) != repository.project_id
        ):
            raise RemoteError(
                'GitLab returned another merge request or target project.', code='source_identity_changed'
            )
        if request.get('state') != 'merged' or request.get('merged_at') is None:
            raise RemoteError('Selected GitLab merge request is not merged.', code='invalid_source_metadata')
        refs = object_field(request.get('diff_refs'), 'diff_refs; retry after source metadata is ready')
        versions = [item for _page, rows in self.client.pages(path + '/versions') for item in rows]
        if not versions:
            raise RemoteError(
                'GitLab did not provide the source diff version; retry after metadata is ready.',
                code='invalid_source_metadata',
            )
        version = max(versions, key=lambda item: positive_id(item.get('id'), 'diff version ID'))
        if any(
            version.get(name) != refs.get(ref)
            for name, ref in [
                ('base_commit_sha', 'base_sha'),
                ('start_commit_sha', 'start_sha'),
                ('head_commit_sha', 'head_sha'),
            ]
        ):
            raise RemoteError(
                'GitLab latest diff version changed while reading the source; rebuild collect.',
                code='source_changed',
            )
        version_id = str(positive_id(version['id'], 'diff version ID'))
        author = request.get('author')
        body = request.get('description') or ''
        if not isinstance(body, str):
            raise RemoteError('GitLab returned an invalid MR description.', code='invalid_source_metadata')
        metadata = ChangeRequest(
            kind='merge_request',
            repository=repository.path,
            number=number,
            url=https_url(text_field(request.get('web_url'), 'MR URL')),
            title=text_field(request.get('title'), 'MR title'),
            body=body,
            author=(
                None
                if author is None
                else text_field(object_field(author, 'author').get('username'), 'author username')
            ),
            merged_at=utc_time(request.get('merged_at')),
        )
        commits = self._commits(repository.project_id, number)
        if not commits:
            raise ChangeRejected(
                RejectedRecord(
                    id=change_id('gitlab', repository.server_url, repository.project_id, 'merge_request', number),
                    change_request=RejectedChangeRequest.model_validate(
                        metadata.model_dump(include=set(RejectedChangeRequest.model_fields))
                    ),
                    reasons=[
                        RejectionReason(
                            code='source_commits_unavailable',
                            message='GitLab returned no source commits for the selected MR.',
                            observed=0,
                            expected='at least one source commit',
                        )
                    ],
                )
            )
        if repository.project_id not in self.fetch_urls:
            self.resolve_repository(repository.project_id)
        source = SourceDescriptor(
            vcs='git',
            fetch_url=self.fetch_urls[repository.project_id],
            fetch_refs=[
                f'refs/merge-requests/{number}/head',
                'refs/heads/' + text_field(request.get('target_branch'), 'target branch'),
            ],
        )
        evidence = SourceRevisions(
            base_sha=text_field(refs.get('start_sha'), 'target/start SHA'),
            head_sha=text_field(refs.get('head_sha'), 'head SHA'),
            expected_base_sha=text_field(refs.get('base_sha'), 'merge-base SHA'),
            merge_commit_sha=request.get('merge_commit_sha'),
            squash_commit_sha=request.get('squash_commit_sha'),
            head_repository=None,
            commits_count=len(commits),
            target_branch=text_field(request.get('target_branch'), 'target branch'),
            diff_version_id=version_id,
        )
        return CollectRecord(
            id=change_id('gitlab', repository.server_url, repository.project_id, 'merge_request', number),
            change_request=metadata,
            revisions=Revisions(source=evidence),
            source=source,
        )

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Return the current complete commit listing for comparison to frozen evidence."""
        project = self._record_repository(record)
        return self._commits(project.project_id, record.change_request.number)

    def _record_repository(self, record: CollectRecord) -> Repository:
        """Bind follow-up API reads to the immutable project encoded by the frozen ID."""
        project = self.projects.get(record.change_request.repository)
        if project is None:
            project = self.resolve_repository(record.change_request.repository)
        identity = change_id(
            'gitlab', project.server_url, project.project_id, 'merge_request', record.change_request.number
        )
        if record.id != identity:
            raise RemoteError('GitLab project identity changed; rebuild collection.', code='source_changed')
        return project

    def _issue_project_path(self, project_id: str) -> str:
        """Resolve an issue project's namespace without requiring Git commits or a branch."""
        project = self.projects.get(project_id)
        if project is not None:
            return project.path
        raw, _headers = self.client.request('projects/' + project_id)
        metadata = object_field(raw, 'issue project')
        if str(positive_id(metadata.get('id'), 'issue project ID')) != project_id:
            raise RemoteError('GitLab returned another issue project ID.', code='source_identity_changed')
        return text_field(metadata.get('path_with_namespace'), 'issue project path')

    def get_closing_issues(self, record: CollectRecord) -> list[Issue]:
        """Normalize accessible closing links, including cross-project issue identity."""
        repository = self._record_repository(record)
        path = (
            'projects/'
            + repository.project_id
            + '/merge_requests/'
            + str(record.change_request.number)
            + '/closes_issues'
        )
        issues = {}
        for _page, rows in self.client.pages(path):
            for row in rows:
                if row.get('iid') is None or row.get('project_id') is None:
                    raise RemoteError(
                        'GitLab issue tracker does not provide native issue fields; use a supported tracker.',
                        code='unsupported_issue_tracker',
                    )
                project_id = str(positive_id(row['project_id'], 'issue project ID'))
                project_path = self._issue_project_path(project_id)
                number = positive_id(row['iid'], 'issue iid')
                body = row.get('description') or ''
                if not isinstance(body, str):
                    raise RemoteError('GitLab returned an invalid issue body.', code='invalid_source_metadata')
                item = Issue(
                    id=issue_id('gitlab', self.config.server_url, project_id, number),
                    repository=project_path,
                    number=number,
                    url=https_url(text_field(row.get('web_url'), 'issue URL')),
                    title=text_field(row.get('title'), 'issue title'),
                    body=body,
                )
                issues[item.id] = item
        return [issues[key] for key in sorted(issues)]
