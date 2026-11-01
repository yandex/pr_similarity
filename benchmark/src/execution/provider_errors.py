"""Classify terminal provider errors without copying untrusted diagnostics."""

import json
from datetime import UTC, datetime
from types import MappingProxyType

from benchmark.json_types import JsonObject
from benchmark.models.report import ReportError

FATAL_PROVIDER_CODES = frozenset({'auth_required', 'credentials_missing', 'provider_quota_exhausted'})
ERROR_PRIORITY = MappingProxyType(
    {
        'auth_required': 0,
        'credentials_missing': 0,
        'provider_quota_exhausted': 1,
        'provider_unavailable': 2,
        'agent_error': 3,
    }
)


def quota_reset_message(body: JsonObject) -> str:
    """Render optional reset hints, rejecting booleans and unrepresentable dates."""
    absolute = body.get('resets_at')
    if type(absolute) is int and absolute > 0:
        try:
            stamp = datetime.fromtimestamp(absolute, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
            return f'Provider-reported reset: {stamp}. Resume manually after quota is available.'
        except (OverflowError, OSError, ValueError):
            pass
    relative = body.get('resets_in_seconds')
    if type(relative) is int and relative >= 0:
        return f'Provider-reported time until reset: {relative} seconds. Resume manually after quota is available.'
    return 'Provider reset time is unavailable. Resume manually after quota is available.'


def classify_provider_error(error: JsonObject, *, oauth_openai: bool) -> ReportError:
    """Keep auth precedence and limit quota-stop to an explicit OAuth provider response."""
    data = error.get('data')
    data = data if isinstance(data, dict) else {}
    status = data.get('statusCode')
    if status in (401, 403) or error.get('name') in ('ProviderAuthError', 'AuthenticationError'):
        return ReportError(code='auth_required', message='Provider rejected credentials; renew them and resume.')
    if oauth_openai and error.get('name') == 'APIError' and type(status) is int and status == 429:
        raw = data.get('responseBody')
        try:
            body = json.loads(raw) if isinstance(raw, str) else None
        except (ValueError, RecursionError):
            body = None
        detail = body.get('error') if isinstance(body, dict) else None
        if isinstance(detail, dict) and detail.get('type') == 'usage_limit_reached':
            return ReportError(
                code='provider_quota_exhausted',
                message='ChatGPT usage limit reached. ' + quota_reset_message(detail),
            )
    if type(status) is int and 500 <= status < 600:
        return ReportError(code='provider_unavailable', message='Provider could not complete the request.')
    return ReportError(code='agent_error', message='OpenCode reported a terminal agent error.')
