"""Exercise both wire protocols against a local HTTP server."""

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from bananavibe.config import Config, Model
from bananavibe.forge import Forge, ForgeError
from bananavibe.state import StateStore


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def call(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        self.server.calls.append((self.command, self.path, self.headers.get('Authorization'), data))
        route = self.path.split('?')[0]
        result, status = {}, 200
        if route.endswith('/user'):
            result = {'login': 'bot', 'id': 7}
            status = getattr(self.server, 'user_status', 200)
        elif route.endswith('/installation/repositories'):
            result = {'repositories': [{'full_name': 'team/prompts'}]}
            status = getattr(self.server, 'installation_status', 200)
        elif '/collaborators/' in route:
            result = {'permission': 'write' if '/maintainer/' in route else 'read'}
        elif route.endswith('/pulls'):
            result = self.server.pulls if self.command == 'GET' else {'html_url': 'https://forge.invalid/pulls/7', 'number': 7, 'head': {'ref': data['head']}}
        elif route.endswith('/pulls/7'):
            result = {**self.server.pulls[0], **data}
        elif '/contents/' in route:
            if self.command == 'GET':
                result = self.server.saved
                status = 404 if result is None else 200
            else:
                assert data['branch'] == 'bananavibe-state'
                self.server.saved = {'sha': 'new-sha', 'content': data['content']}
                result = self.server.saved
        elif '/git/ref/' in route:
            result = {'object': {'sha': 'a' * 40}}
        elif '/branches/' in route:
            result = {'commit': {'id': 'a' * 40}}
        elif route.endswith('/branches') or route.endswith('/git/refs'):
            status = 201
        elif '/repos/' in route:
            result = {'default_branch': 'main', 'permissions': {'push': True}}
        raw = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = call


@pytest.fixture
def api():
    server = ThreadingHTTPServer(('127.0.0.1', 0), API)
    server.calls, server.saved, server.pulls = [], None, []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.mark.parametrize('provider', ['github', 'forgejo'])
def test_permissions_state_branch_pr_and_closure_use_each_forge_protocol(api, provider):
    url = f'http://127.0.0.1:{api.server_port}'
    model = Model('fixture', 'openai-compatible', 'model', 'https://models.invalid', '')
    config = Config(provider, url, url + ('/api/v1' if provider == 'forgejo' else ''), 'team/prompts', 'suite/BananaWiki',
                    'main', {'fixture': model}, 'fixture', [], [['true']], allow_http=True)
    forge = Forge(config, 'fixture-secret')
    assert forge.authorized('maintainer')
    assert not forge.authorized('stranger')
    assert forge.branch_sha(config.target_repository, 'main') == 'a' * 40
    forge.create_branch(config.target_repository, 'bananavibe/opaque-1', 'a' * 40, source_branch='main')
    store = StateStore(forge)
    store.mutate(1, lambda _: {'schema': 1, 'issue': 1, 'status': 'queued'})
    store.mutate(1, lambda old: {**old, 'status': 'running'})
    assert store.read(1)[0]['status'] == 'running'
    forge.pull_request('bananavibe/opaque-1', 'Fix behavior', 'Source-only summary')
    forge.comment(1, 'Ready for review')
    forge.close_issue(1)
    authorization = ('token ' if provider == 'forgejo' else 'Bearer ') + 'fixture-secret'
    assert all(call[2] == authorization for call in api.calls)
    creation = next(call for call in api.calls if call[1].endswith('/pulls') and call[0] == 'POST')
    if provider == 'github':
        assert creation[3]['draft'] is True
        assert any(call[1].endswith('/git/refs') and call[3]['ref'] == 'refs/heads/bananavibe/opaque-1' for call in api.calls)
    else:
        assert creation[3]['title'].startswith('WIP: ')
        assert 'draft' not in creation[3]
        assert any(call[1].endswith('/branches') and call[3]['old_ref_name'] == 'a' * 40 for call in api.calls)
        writes = [call[0] for call in api.calls if '/contents/' in call[1] and call[0] != 'GET']
        assert writes == ['POST', 'PUT']
    assert api.calls[-1][0] == 'PATCH' and api.calls[-1][3] == {'state': 'closed'}


@pytest.mark.parametrize('state', ['open', 'closed', 'merged'])
def test_resumed_publication_updates_open_pr_and_never_reuses_a_closed_pr(api, state):
    url = f'http://127.0.0.1:{api.server_port}'
    model = Model('fixture', 'openai-compatible', 'model', 'https://models.invalid', '')
    config = Config('github', url, url, 'team/prompts', 'suite/BananaWiki', 'main', {'fixture': model}, 'fixture', [], [['true']], allow_http=True)
    forge = Forge(config, 'fixture-secret')
    api.pulls = [{'number': 7, 'state': 'open' if state == 'open' else 'closed', 'merged': state == 'merged',
                  'head': {'ref': 'bananavibe/task'}, 'html_url': 'https://forge.invalid/pulls/7', 'body': 'Earlier validation'}]
    if state == 'open':
        result = forge.pull_request('bananavibe/task', 'Updated contribution', 'New validation for the latest checkpoint')
        assert result['body'] == 'New validation for the latest checkpoint'
        assert not any(method == 'POST' and '/pulls' in path for method, path, *_ in api.calls)
    else:
        with pytest.raises(ValueError, match='closed or merged'):
            forge.pull_request('bananavibe/task', 'Updated contribution', 'New validation')
        assert not any(method in {'PATCH', 'POST'} and '/pulls' in path for method, path, *_ in api.calls)


def test_github_installation_token_can_read_backups_without_a_user_profile(api):
    url = f'http://127.0.0.1:{api.server_port}'
    model = Model('fixture', 'openai-compatible', 'model', 'https://models.invalid', '')
    config = Config('github', url, url, 'team/prompts', 'suite/BananaWiki', 'main',
                    {'fixture': model}, 'fixture', [], [['true']], allow_http=True)
    api.user_status = 403
    forge = Forge(config, 'fixture-installation-token')
    assert forge.identity['login'] == 'x-access-token'
    assert forge.authorized('maintainer')
    assert any(path == '/installation/repositories?per_page=1' for _, path, *_ in api.calls)
    from bananavibe.controller import Controller
    assert Controller(forge).event('issue_comment', {
        'action': 'created', 'issue': {'number': 7},
        'sender': {'login': 'maintainer', 'type': 'Bot'},
        'comment': {'id': 123, 'body': '/banana restart'},
    }) is None
    assert not any(method != 'GET' for method, *_ in api.calls)


@pytest.mark.parametrize('provider,user_status,installation_status', [
    ('github', 401, 200), ('github', 403, 403), ('forgejo', 403, 200),
])
def test_profile_errors_do_not_bypass_forge_authentication(api, provider, user_status, installation_status):
    url = f'http://127.0.0.1:{api.server_port}'
    model = Model('fixture', 'openai-compatible', 'model', 'https://models.invalid', '')
    config = Config(provider, url, url, 'team/prompts', 'suite/BananaWiki', 'main',
                    {'fixture': model}, 'fixture', [], [['true']], allow_http=True)
    api.user_status, api.installation_status = user_status, installation_status
    with pytest.raises(ForgeError):
        Forge(config, 'fixture-invalid-token')
    if provider != 'github' or user_status != 403:
        assert not any('/installation/' in path for _, path, *_ in api.calls)
