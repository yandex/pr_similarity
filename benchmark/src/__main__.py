"""Internal Python entrypoint; Make owns the user-facing operation interface."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from benchmark.auth import native_login
from benchmark.config import load_config
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.provisioning import provision_opencode
from benchmark.publish import publish
from benchmark.report import read_report
from benchmark.repricing import reprice
from benchmark.rescore import rescore
from benchmark.runner import run
from benchmark.visualize import compare, visualize
from dotenv import load_dotenv


class SafeParser(argparse.ArgumentParser):
    """Validate CLI arguments without echoing credential values."""

    def error(self, message: str) -> None:
        """Reject invalid command arguments without printing their values."""
        self.exit(
            2, 'Invalid arguments; use --help for accepted options. Credentials must not be supplied as literals.\n'
        )


def parser() -> argparse.ArgumentParser:
    """Build the internal CLI and Make environment defaults."""

    def env(name: str) -> str | None:
        """Read an optional Make parameter from the process environment."""
        return os.environ.get('PRSIM_BM_' + name) or None

    result = SafeParser(description='Benchmark frozen Git coding tasks with OpenCode.')
    commands = result.add_subparsers(dest='command', required=True)
    launch = commands.add_parser('run')
    launch.add_argument('--config', type=Path, default=env('CONFIG') or 'config.yaml')
    launch.add_argument('--dataset', type=Path, default=env('DATASET') or 'basket.json')
    launch.add_argument('--output', type=Path, default=env('OUTPUT'))
    launch.add_argument('--resume', type=Path, default=env('RESUME'))
    launch.add_argument('--workers', type=int, default=env('WORKERS'))
    launch.add_argument('--reprice', choices=('0', '1'), default=env('REPRICE') or '0')
    launch.add_argument('--pricing', type=Path, default=env('PRICING'))
    launch.add_argument('--progress', choices=('auto', 'plain', 'off'), default=env('PROGRESS') or 'auto')
    price = commands.add_parser('reprice')
    price.add_argument('--run', type=Path, default=env('RUN'))
    price.add_argument('--pricing', type=Path, default=env('PRICING'))
    score = commands.add_parser('rescore')
    score.add_argument('--run', type=Path, default=env('RUN'))
    auth = commands.add_parser('auth')
    auth.add_argument('--config', type=Path, default=env('CONFIG') or 'config.yaml')
    auth.add_argument('--flow', choices=('headless', 'browser'), default=env('AUTH_FLOW') or 'headless')
    view = commands.add_parser('visualize')
    view.add_argument('--run', type=Path, default=env('RUN'))
    view.add_argument('--no-browser', choices=('0', '1'), default=env('NO_BROWSER') or '0')
    comparison = commands.add_parser('compare')
    comparison.add_argument('--runs-json', default=env('RUNS_JSON'))
    comparison.add_argument('--no-browser', choices=('0', '1'), default=env('NO_BROWSER') or '0')
    upload = commands.add_parser('publish')
    upload.add_argument('--run', type=Path, default=env('RUN'))
    upload.add_argument('--url', default=env('URL'))
    upload.add_argument('--dry-run', choices=('0', '1'), default=env('DRY_RUN') or '0')
    upload.add_argument('--overwrite', choices=('0', '1'), default=env('OVERWRITE') or '0')
    upload.add_argument('--token-file', type=Path, default=env('TOKEN_FILE'))
    upload.add_argument('--token-env', default=env('TOKEN_ENV'))
    upload.add_argument('--token-prompt', choices=('0', '1'), default=env('TOKEN_PROMPT') or '0')
    upload.add_argument('--progress', choices=('auto', 'plain', 'off'), default=env('PROGRESS') or 'auto')
    for sub in (launch, price, score, auth, view, comparison, upload):
        sub.add_argument(
            '--log-level', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'), default=env('LOG_LEVEL') or 'INFO'
        )
    return result


def _launch(args: argparse.Namespace) -> int:
    """Validate operation inputs and execute the requested command."""
    if args.progress not in ('auto', 'plain', 'off'):
        raise BenchmarkError('invalid_progress', 'PROGRESS must be auto, plain or off.')
    if args.reprice not in ('0', '1'):
        raise BenchmarkError('invalid_arguments', 'REPRICE accepts only 0 or 1.')
    if args.resume is not None and args.output is not None:
        raise BenchmarkError('invalid_arguments', 'RESUME and explicit OUTPUT cannot be combined.')
    if args.reprice == '1' and args.pricing is None:
        raise BenchmarkError('pricing_missing', 'REPRICE=1 requires PRICING.')
    if args.reprice == '0' and args.pricing is not None:
        raise BenchmarkError('pricing_not_enabled', 'Set REPRICE=1 when supplying PRICING to run.')
    config = load_config(args.config, workers=args.workers)
    status, directory = run(
        config,
        args.dataset,
        output=args.output or Path('./output'),
        resume=args.resume,
        level=args.log_level,
        pricing=args.pricing,
        progress=args.progress,
    )
    report = read_report(directory)
    print(f'Agent run: {report['run']['status']}; report: {directory / 'report.json'}.', file=sys.stderr)
    return status


def _view(args: argparse.Namespace) -> int:
    """Validate operation inputs and execute the requested command."""
    if args.no_browser not in ('0', '1'):
        raise BenchmarkError('invalid_arguments', 'NO_BROWSER accepts only 0 or 1.')
    if args.command == 'visualize':
        if args.run is None:
            raise BenchmarkError('invalid_arguments', 'Visualize requires RUN.')
        path = visualize(args.run, no_browser=args.no_browser == '1', level=args.log_level)
    else:
        if args.runs_json is None:
            raise BenchmarkError('invalid_arguments', 'Compare requires RUNS_JSON with at least two paths.')
        try:
            runs = json.loads(args.runs_json)
        except ValueError as error:
            raise BenchmarkError('invalid_arguments', 'RUNS_JSON must be a valid JSON array of run paths.') from error
        if (
            not isinstance(runs, list)
            or len(runs) < 2
            or any(not isinstance(item, str) or not item.strip() for item in runs)
        ):
            raise BenchmarkError('invalid_arguments', 'RUNS_JSON must contain at least two nonempty path strings.')
        path = compare([Path(item) for item in runs], no_browser=args.no_browser == '1', level=args.log_level)
    print(f'Saved local HTML: {path}', file=sys.stderr)
    return 0


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch an operation after common argument validation."""
    if args.command == 'run':
        return _launch(args)
    if args.command == 'reprice':
        if args.run is None or args.pricing is None:
            raise BenchmarkError('invalid_arguments', 'Reprice requires RUN and PRICING.')
        return reprice(args.run.expanduser().resolve(), args.pricing, level=args.log_level)
    if args.command == 'rescore':
        if args.run is None:
            raise BenchmarkError('invalid_arguments', 'Rescore requires RUN.')
        return rescore(args.run.expanduser().resolve(), level=args.log_level)
    if args.command == 'auth':
        if args.flow not in ('headless', 'browser'):
            raise BenchmarkError('invalid_arguments', 'AUTH_FLOW accepts headless or browser.')
        config = load_config(args.config)
        if config.data['agent']['auth']['mode'] != 'chatgpt_oauth':
            raise BenchmarkError('auth_mode', 'make auth requires agent.auth.mode=chatgpt_oauth.')
        runtime = provision_opencode(
            config.data['agent']['runtime'],
            Path(config.data['provisioning']['cache_dir']),
            timeout=config.data['provisioning']['download_timeout_seconds'],
        )
        return native_login(runtime.binary, Path(config.data['agent']['auth']['profile']), flow=args.flow)
    if args.command in ('visualize', 'compare'):
        return _view(args)
    if args.command == 'publish':
        if args.run is None or args.url is None:
            raise BenchmarkError('invalid_arguments', 'Publish requires RUN and an explicit HTTPS URL.')
        if any(value not in ('0', '1') for value in (args.dry_run, args.overwrite, args.token_prompt)):
            raise BenchmarkError('invalid_arguments', 'DRY_RUN, OVERWRITE and TOKEN_PROMPT accept only 0 or 1.')
        return publish(
            args.run,
            args.url,
            dry_run=args.dry_run == '1',
            overwrite=args.overwrite == '1',
            token_file=args.token_file,
            token_env=args.token_env,
            token_prompt=args.token_prompt == '1',
            progress=args.progress,
            level=args.log_level,
        )
    return 0


def main() -> int:
    """Dispatch a validated operation and keep console failures safe."""
    if os.environ.get('PYTHONHASHSEED') != '42':
        environment = os.environ.copy()
        environment['PYTHONHASHSEED'] = '42'
        os.execve(sys.executable, [sys.executable, '-m', 'benchmark', *sys.argv[1:]], environment)
    args = parser().parse_args()
    load_dotenv(Path.cwd() / '.env', override=False)
    try:
        if os.environ.get('PRSIM_BM_UNSUPPORTED', '').strip():
            raise BenchmarkError(
                'unsupported_parameter',
                'Unsupported Make parameter: task selectors and literal credentials/owner are forbidden.',
            )
        required = ('CONFIG', 'DATASET') if args.command == 'run' else ('CONFIG',) if args.command == 'auth' else ()
        for name in required:
            if 'PRSIM_BM_' + name in os.environ and not os.environ['PRSIM_BM_' + name].strip():
                raise BenchmarkError('invalid_arguments', f'{name} must not be empty.')
        if args.log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
            raise BenchmarkError('invalid_arguments', 'LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.')
        return dispatch(args)
    except BenchmarkError as error:
        print(error.message, file=sys.stderr)
        return error.exit_code
    except (OSError, ValueError, KeyError, TypeError):
        print('Invalid input or inaccessible artifacts; check arguments and the DEBUG file log.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted; saved runs can be continued with RESUME.', file=sys.stderr)
        return 130
    except OPERATION_ERRORS:
        logging.getLogger('benchmark.diagnostics').debug('Unhandled command error', exc_info=True)
        print('Unexpected benchmark error; check the operation DEBUG log. Saved runs can be resumed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
