"""Elapsed output stays bounded, prompt-safe and independent of durable upload results."""

import io
import threading

import httpx
from benchmark.publication_progress import PublicationProgress
from benchmark.publish import publish
from test_archive import complete_run
from test_publish import TOKEN, receipt, viewer_transport


def test_plain_elapsed_beyond_two_minutes_and_prompt_pause():
    now = [0.0]
    stream = io.StringIO()
    feedback = PublicationProgress('plain', stream=stream, clock=lambda: now[0])
    try:
        feedback.phase('Publishing · sending archive')
        now[0] = 9
        feedback.tick()
        assert stream.getvalue().count('\n') == 1
        now[0] = 121
        feedback.phase('Publishing · waiting for server', reset=False)
        assert 'elapsed 2m 01s' in stream.getvalue()
        feedback.phase('Waiting for login', active=False)
        before = stream.getvalue()
        now[0] = 150
        feedback.tick()
        assert stream.getvalue() == before
    finally:
        feedback.close()
    assert feedback.thread is not None and not feedback.thread.is_alive()


def test_tty_off_and_broken_output_do_not_fail_work():
    class TTY(io.StringIO):
        def isatty(self):
            return True

    for mode in ('auto', 'off'):
        stream = TTY()
        feedback = PublicationProgress(mode, stream=stream)
        feedback.phase('Preparing archive')
        feedback.write_message('A durable warning')
        feedback.close()
        assert feedback.thread is None or not feedback.thread.is_alive()
        assert ('\r\x1b[2K' in stream.getvalue()) == (mode == 'auto')
        assert 'A durable warning\n' in stream.getvalue()
    stream = io.StringIO()
    stream.close()
    feedback = PublicationProgress('auto', stream=stream)
    feedback.phase('Publishing')
    feedback.close()


def test_upload_timeout_does_not_expand_readback(tmp_path, monkeypatch, capsys):
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)

    def upload(request):
        assert request.extensions['timeout']['read'] == 960
        assert request.extensions['timeout']['connect'] == 20
        return httpx.Response(201, json=receipt(request))

    transport = viewer_transport(upload)

    def handler(request):
        if request.method == 'GET':
            assert request.extensions['timeout']['read'] == 120
        return transport.handle_request(request)

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=120) as client:
        assert (
            publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client, progress='plain') == 0
        )
    output = capsys.readouterr().err
    assert 'Publishing to object storage may take several minutes' in output
    assert 'waiting for server' in output and 'Verifying publication' in output
    assert not any(t.name == 'publication-progress' for t in threading.enumerate())


def test_unavailable_timer_does_not_prevent_confirmed_publication(tmp_path, monkeypatch):
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)

    def fail_start(self):
        raise RuntimeError('No progress thread available')

    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with httpx.Client(transport=viewer_transport(lambda request: httpx.Response(201, json=receipt(request)))) as client:
        assert publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client) == 0
