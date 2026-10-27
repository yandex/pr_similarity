"""Pure basket, duration and harness metadata projections shared by the viewer."""

from viewer.config import Limits
from viewer.models import Basket
from viewer.models.report import ReportRun
from viewer.util import utc_timestamp


def basket_metadata(basket: Basket) -> dict:
    """Expose only display metadata from the frozen collector lineage."""
    meta = basket.meta
    settings = next(
        (entry.settings for entry in reversed(meta.lineage or []) if entry.stage == 'generate'),
        {},
    )
    source = settings.get('source') if settings.get('source') in ('issue', 'llm') else None
    context = (
        settings.get('context')
        if source == 'llm' and settings.get('context') in ('diff', 'issue', 'diff+issue')
        else None
    )
    return {
        'name': meta.name,
        'description': meta.description,
        'task_source': {'source': source, 'context': context},
    }


def wall_time(run: ReportRun) -> float | None:
    """Return execution wall time without later report edits or pricing operations."""
    if run.finished_at is None:
        return None
    duration = (utc_timestamp(run.finished_at) - utc_timestamp(run.created_at)).total_seconds()
    return duration if duration >= 0 else None


def projection(identity: dict, limits: Limits, inference: object = None) -> dict:
    """Project harness controls for the run page, independently of native config."""

    def integer(value: object, minimum: int = 0) -> int | None:
        """Validate one bounded integer while preserving missing-value semantics."""
        if value is None:
            return None
        if type(value) is not int or not minimum <= value <= 2**31:
            raise ValueError('unsupported_metadata')
        return value

    runtime = identity.get('runtime')
    if runtime is not None and (not isinstance(runtime, str) or not runtime or len(runtime) > limits.runtime_chars):
        raise ValueError('unsupported_metadata')
    setup, retries = identity.get('setup'), identity.get('retries')
    if setup is not None and not isinstance(setup, dict):
        raise ValueError('unsupported_metadata')
    if retries is not None and not isinstance(retries, dict):
        raise ValueError('unsupported_metadata')
    excludes = identity.get('diff_excludes')
    categories = retries.get('categories') if retries is not None else None
    for values in (excludes, categories):
        if values is not None and (
            not isinstance(values, list)
            or len(values) > 1024
            or any(not isinstance(v, str) or not v or len(v) > 4096 for v in values)
        ):
            raise ValueError('unsupported_metadata')
    return {
        'runtime': runtime,
        'timeout_seconds': integer(identity.get('timeout_seconds'), 1),
        'setup_timeout_seconds': integer(setup.get('timeout_seconds'), 1) if setup else None,
        'retries': {'max_retries': integer(retries.get('max_retries')), 'categories': categories} if retries else None,
        'diff_excludes': excludes,
    }
