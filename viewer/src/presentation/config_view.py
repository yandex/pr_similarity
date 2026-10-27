"""One safe native OpenCode document, separately from harness metadata."""

from viewer.errors import ViewerError
from viewer.presentation import run_projection
from viewer.presentation.native_config import native_projection as native_projection
from viewer.service import ViewerService
from viewer.util import digest, json_bytes, parse_json
from viewer.validation.contracts import require

projection = run_projection.projection


def public_config(
    service: ViewerService, rid: str, expected: str | None = None, publication: str | None = None
) -> dict:
    """Read one digest-bound input while publication mutations hold the same lock."""
    with service.lock:
        report_sha = service.check_report(rid, expected, publication)
        report = service.full_report(rid)
        row = service._require_run(rid)
        source = report.agent_input.config
        result = {
            'projection_version': 2,
            'run_id': rid,
            'report_sha256': report_sha,
            'publication_sha256': row['marker']['archive_sha256'],
            'identity_sha256': report.config.identity_sha256,
            'source': source.to_wire(),
            'supported': False,
            'config': None,
            'reason': None,
        }
        require(source.path == 'agent/opencode.json', 'invalid_agent_input')
        try:
            raw = service.store.get(row['prefix'] + 'data/' + source.path, service.config.limits.json_bytes)
        except ViewerError as error:
            if error.status in (404, 413):
                return {**result, 'reason': 'snapshot_unavailable'}
            raise
        require(len(raw) == source.size_bytes and digest(raw) == source.sha256, 'stored_digest_mismatch')
        try:
            value = native_projection(parse_json(raw))
        except ValueError as error:
            return {**result, 'reason': str(error)}
        if len(json_bytes(value)) > service.config.limits.config_display_bytes:
            return {**result, 'reason': 'display_limit'}
        return {**result, 'supported': True, 'config': value}
