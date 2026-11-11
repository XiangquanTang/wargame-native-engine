"""Offline fake-runtime tests: no model loading, downloads or dependencies."""
import ast
import http.client
import json
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from serve_qwen_strategy import QwenRuntime, RequestError, StrategyServer, handler_for, validate_request


class FakeRuntime:
    def health(self):
        return {'status': 'ok', 'fake': True}

    def plan(self, messages, limit):
        text = messages[-1]['content']
        if text == 'context':
            raise RequestError(400, 'context_limit', 'Fake context rejection')
        if text == 'oom':
            raise RequestError(503, 'cuda_out_of_memory', 'Fake OOM')
        if text == 'crash':
            raise RuntimeError('sensitive prompt must not be exposed')
        return {'text': '测试响应', 'input_tokens': 12, 'output_tokens': 4, 'finish_reason': 'stop', 'generation_seconds': 0.001}


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = StrategyServer(('127.0.0.1', 0), handler_for(FakeRuntime()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method='POST', path='/api/plan', payload=None, raw=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        body = raw if raw is not None else json.dumps(payload or {'messages': [{'role': 'user', 'content': '你好'}]}).encode()
        connection.request(method, path, body=None if method == 'GET' else body,
                           headers={'Content-Type': 'application/json'} if headers is None else headers)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def test_health(self):
        self.assertEqual(self.request('GET', '/api/health'), (200, {'status': 'ok', 'fake': True}))

    def test_real_http_fake_runtime(self):
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(result['text'], '测试响应')

    def test_routes(self):
        self.assertEqual(self.request(path='/v1/chat/completions')[0], 404)
        self.assertEqual(self.request('GET', '/')[0], 404)

    def test_json(self):
        for body in [b'{', b'\xff', b'{"max_new_tokens": NaN}', b'[' * 1500]:
            self.assertEqual(self.request(raw=body)[0], 400)

    def test_limits_and_roles(self):
        invalid = [None, [], {}, {'messages': []}, {'messages': ['x']},
                   {'messages': [{'role': 'tool', 'content': 'x'}]},
                   {'messages': [{'role': 'user', 'content': ''}]},
                   {'messages': [{'role': 'user', 'content': 'x' * 32769}]},
                   {'messages': [{'role': 'assistant', 'content': 'x'}]}]
        for data in invalid:
            with self.assertRaises(RequestError):
                validate_request(data)
        for limit in [True, 0, 1025, 1.5, float('inf'), '128']:
            with self.assertRaises(RequestError):
                validate_request({'messages': [{'role': 'user', 'content': 'x'}], 'max_new_tokens': limit})
        with self.assertRaises(RequestError):
            validate_request({'messages': [{'role': 'user', 'content': 'x'}], 'temperature': 0})

    def test_defaults_and_maximum(self):
        messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}]
        self.assertEqual(validate_request({'messages': messages})[1], 256)
        self.assertEqual(validate_request({'messages': messages, 'max_new_tokens': 1024})[1], 1024)

    def test_error_mapping(self):
        for content, status in [('context', 400), ('oom', 503), ('crash', 500)]:
            actual, result = self.request(payload={'messages': [{'role': 'user', 'content': content}]})
            self.assertEqual(actual, status)
            self.assertIn('error', result)
            self.assertNotIn('sensitive', json.dumps(result))

    def test_body_limit_and_content_type(self):
        self.assertEqual(self.request(raw=b'x' * 131073)[0], 413)
        self.assertEqual(self.request(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request(headers={'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'})[0], 400)

    def test_oom_cleanup_wrapper(self):
        class FakeOOM(Exception):
            pass
        cleared = []
        runtime = QwenRuntime.__new__(QwenRuntime)
        runtime.torch = SimpleNamespace(cuda=SimpleNamespace(
            OutOfMemoryError=FakeOOM, empty_cache=lambda: cleared.append(True)))
        def fail(*args):
            raise FakeOOM('test')
        runtime._plan_impl = fail
        with self.assertRaises(RequestError) as caught:
            runtime.plan([], 1)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(cleared, [True])

    def test_python310_syntax(self):
        for path in Path(__file__).parent.glob('*.py'):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 10))


if __name__ == '__main__':
    unittest.main(verbosity=2)
