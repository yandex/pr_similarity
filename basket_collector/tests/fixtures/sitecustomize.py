"""Offline subprocess seam for the Make smoke test only.

Python imports this module when the test puts this directory on PYTHONPATH.
The environment gate keeps ordinary package execution untouched.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from basket_collector.models.artifacts import Issue, Repository
from basket_collector.models.sources import SourceDescriptor, issue_id, source_hash
from basket_collector.providers import github_source
from basket_collector.vcs.git import GitRepository as RealGitRepository

_FIXTURE = os.environ.get('BASKET_COLLECTOR_MAKE_FIXTURE')

if _FIXTURE:
    _DATA: dict[str, Any] = json.loads(Path(_FIXTURE).read_text(encoding='utf-8'))

    def _record(operation: str) -> None:
        """Append an operation name to the optional smoke request log."""
        log_path = os.environ.get('BASKET_COLLECTOR_MAKE_LOG')
        if log_path:
            with Path(log_path).open('a', encoding='utf-8') as stream:
                stream.write(operation + '\n')

    class LocalGitHubClient:
        """Serve deterministic GitHub data from the subprocess fixture."""

        def __init__(self, config: object, *, stop_event: threading.Event | None = None) -> None:
            """Keep the injected stop event for compatibility with the real client."""
            self.stop_event = stop_event

        def close(self) -> None:
            """Close the fake client without external resources."""
            return None

        def repository(self, full_name: str) -> Repository:
            """Return the local fixture repository as a public GitHub repository."""
            _record('repository')
            return Repository(
                path=full_name,
                web_url=f'https://github.com/{full_name}',
                default_branch='develop',
                provider='github',
                server_url='https://github.com',
                project_id='123',
            )

        def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
            """Return the configured fixture PR snapshot."""
            _record(f'pr:{number}')
            item = _DATA['tasks'][str(number)]
            return {
                'number': number,
                'merged_at': item['merged_at'],
                'html_url': f'https://github.com/{repository}/pull/{number}',
                'title': item['title'],
                'body': item['body'],
                'user': {'login': 'smoke-author'},
                'commits': 1,
                'merge_commit_sha': None,
                'base': {'sha': item['base'], 'ref': 'develop', 'repo': {'full_name': repository, 'id': 123}},
                'head': {'sha': item['head'], 'repo': {'full_name': repository, 'id': 123}},
            }

        def get_pull_request_commits(self, repository: str, number: int) -> list[dict[str, str]]:
            """Return the saved head SHA as the PR commit list."""
            _record(f'commits:{number}')
            return [{'sha': _DATA['tasks'][str(number)]['head']}]

        def get_closing_issues(self, repository: str, number: int) -> list[Issue]:
            """Return a linked issue, optionally delaying the first request."""
            _record(f'issues:{number}')
            delay = float(os.environ.get('BASKET_COLLECTOR_MAKE_DELAY_SECONDS', '0'))
            if delay and number == 1:
                print('SMOKE_ACTIVE_OPERATION', flush=True)
                time.sleep(delay)
            item = _DATA['tasks'][str(number)]
            return [
                Issue(
                    id=issue_id('github', 'https://github.com', '123', 100 + number),
                    repository=repository,
                    number=100 + number,
                    url=f'https://github.com/{repository}/issues/{100 + number}',
                    title=item['issue_title'],
                    body=item['issue_body'],
                )
            ]

    def local_fetch(self: RealGitRepository, reference: str, depth: int) -> None:
        """Deliver both warm and cold source packs without external network access."""
        assert self.clone_url == 'https://github.com/smoke/repo.git'
        for number, item in _DATA['tasks'].items():
            source = SourceDescriptor(
                vcs='git',
                fetch_url=self.clone_url,
                fetch_refs=[f'refs/pull/{number}/head', 'refs/heads/develop'],
            )
            if item.get('history_unavailable') and reference == item['head'] and self.path.name == source_hash(source):
                _record(f'history_unavailable:{number}')
                self._raise_command_error(
                    ('fetch', '--depth=' + str(depth), self.clone_url, reference),
                    f'fatal: object not found: unshallow {reference}\n'.encode(),
                    True,
                )
        self._run('fetch', '--no-tags', '--depth=' + str(depth), _DATA['local_repository'], reference)

    github_source.GitHubClient = LocalGitHubClient
    RealGitRepository._fetch = local_fetch
