"""Configured endpoint credentials may never travel through HTTP redirects."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import artifact, runtime, completion


@pytest.mark.parametrize('with_tools', [False, True])
@pytest.mark.parametrize('status', [302, 303])
def test_inference_does_not_forward_credentials_to_redirect_destination(with_tools, status):
    forwarded = []
    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            forwarded.append(self.headers.get('Authorization'))
            _, body = completion()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET
    destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.send_response(status)
            self.send_header('Location', f'http://127.0.0.1:{destination.server_port}/elsewhere')
            self.send_header('Content-Length', '0')
            self.end_headers()
    origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (origin, destination)]
    for thread in threads:
        thread.start()
    try:
        result = runtime(EndpointConfig(f'http://127.0.0.1:{origin.server_port}/v1', 'test',
            api_key_env='TEST_ENDPOINT_KEY', features={'streaming': False})).run(
            artifact('read' if with_tools else ''), env={'TEST_ENDPOINT_KEY': 'synthetic-secret'},
            policy=RunPolicy(allowed_tools=frozenset({'read_file'}) if with_tools else frozenset(), read_roots=('.',)))
        assert forwarded == []
        assert result.exit_code == status and result.metadata['provider_error_code'] == 'http_error'
    finally:
        for server in (origin, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
