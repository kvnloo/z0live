"""Local-only llama.cpp transport for the offline lane evals."""
import argparse
import json
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalModel:
    def __init__(self, port, model):
        if not 1 <= port <= 65535:
            raise ValueError('invalid loopback port')
        self.base = f'http://127.0.0.1:{port}'
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.model = model
        models = self.request('/v1/models')['data']
        self.info = next((row for row in models if row['id'] == model), None)
        if self.info is None:
            raise ValueError('requested model is not loaded on the local server')

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, {'Content-Type': 'application/json'})
        try:
            with self.http.open(req, timeout=120) as response:
                return json.load(response)
        except (OSError, ValueError) as exc:
            # Never include server response bodies or private prompts in exceptions/receipts.
            status = f' HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else ''
            raise RuntimeError(f'local {path} failed: {type(exc).__name__}{status}') from None

    def ask(self, system, user, max_tokens, *, literal=False):
        body = {'model': self.model, 'messages': [{'role': 'system', 'content': system},
                                                 {'role': 'user', 'content': user}],
                'temperature': 0, 'seed': 42, 'max_tokens': max_tokens, 'cache_prompt': False,
                'chat_template_kwargs': {'enable_thinking': False}}
        if literal:
            body['grammar'] = 'root ::= "HALLUCINATION" | "CLEAN" | "UNCERTAIN"'
        result = self.request('/v1/chat/completions', body)
        choice = result['choices'][0]
        if choice['finish_reason'] != 'stop':
            raise RuntimeError('local reply did not finish normally')
        return (choice['message']['content'] or '').strip(), result['usage']['prompt_tokens']

    def count(self, text):
        return len(self.request('/tokenize', {'content': text, 'add_special': False})['tokens'])


def local_args(parser: argparse.ArgumentParser):
    parser.add_argument('--port', type=int, default=18087, help='dedicated loopback llama-server port')
    parser.add_argument('--model', default='gemma4-e4b', help='expected loaded model ID')


def rate(n, total):
    return {'n': n, 'total': total, 'rate': n / total if total else None}
