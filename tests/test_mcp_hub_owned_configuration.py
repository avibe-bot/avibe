"""Shared owner contracts: global/project configuration must not bypass publication.

Existing project parsing tests don't exercise global management or admission during
secret writes. Remote discovery is the only stub; storage, admission and commands
use their real owners under conftest's temporary home.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from mcp.types import Tool

from core.mcp_hub import native, repository, scope
from core.mcp_hub.errors import McpHubError
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import save_config


@pytest.fixture(params=['global', 'project'])
def owned(request, tmp_path, monkeypatch):
    save_config(tmp_path)
    root = tmp_path / 'project'
    root.mkdir()
    (root / '.mcp.json').write_text('{"mcpServers": {}}')
    selected = scope.Scope() if request.param == 'global' else scope.Scope('project:test', root)
    monkeypatch.setattr(scope, 'request_scopes', lambda _: [scope.Scope(), selected] if selected.project_root else [selected])
    runtime = McpRuntime()

    async def discover(*_):
        return [Tool(name='ping', inputSchema={'type': 'object'})], SimpleNamespace(version='1'), None

    monkeypatch.setattr(runtime, '_discover', discover)
    return runtime, selected


async def run(runtime, operation, **payload):
    status, body = await runtime.handle(operation, payload)
    assert status == 200
    return json.loads(body)


async def add(owned, entry, name='service'):
    runtime, selected = owned
    result = await run(runtime, 'add', servers=[{'name': name, 'server': entry}])
    assert result['ok'], result
    if selected.project_root:
        result = await run(runtime, 'approve', name=name)
        assert result['ok'], result
    return scope.ScopedName(selected, name)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['needs_config', 'unchanged', 'exists'])
async def test_shared_templates_configure_clear_and_failed_discovery(owned, monkeypatch, name):
    runtime, selected = owned
    key = await add(owned, {'url': '${URL}', 'headers': {'Authorization': 'Bearer ${TOKEN}', 'X-Literal': 'private-header'}}, name)
    detail = await run(runtime, 'detail', name=name)
    assert detail['server']['state'] == 'needs_config'
    assert detail['server']['missingVariables'] == ['TOKEN', 'URL']
    assert detail['server']['url'] == '${URL}'
    assert 'private-header' not in json.dumps(detail)
    assert 'private-header' not in json.dumps(scope._raw_entry(key))
    assert (await run(runtime, 'call', name=name, tool='ping'))['error']['type'] == 'needs_config'

    secrets = {'env': {'URL': 'https://example.invalid/private-route', 'TOKEN': 'private-token'}}
    assert (await run(runtime, 'configure', name=name, secrets=secrets))['ok']
    assert McpRuntime._admit(key, 'ping', {})['definition']['transport']['headers'] == {
        'Authorization': 'Bearer private-token', 'X-Literal': 'private-header',
    }
    detail = await run(runtime, 'detail', name=name)
    assert detail['server']['state'] == 'ok'
    assert all(value not in json.dumps(detail) for value in ['private-token', 'private-route', 'private-header'])

    # An accepted rotation cannot fall back to the previous published tools.
    async def fail(*_):
        raise McpHubError('connection', 'HTTP failure at https://example.invalid/private-route with rotated')
    monkeypatch.setattr(runtime, '_discover', fail)
    rotated = await run(runtime, 'configure', name=name, secrets={'env': {**secrets['env'], 'TOKEN': 'rotated'}})
    assert not rotated['ok']
    assert 'private-route' not in json.dumps(rotated)
    assert 'rotated' not in json.dumps(rotated)
    assert (await run(runtime, 'detail', name=name))['server']['state'] == 'not_generated'
    assert (await run(runtime, 'call', name=name, tool='ping'))['error']['type'] == 'not_generated'
    assert name in {row['name'] for row in (await run(runtime, 'list'))['servers']}

    assert (await run(runtime, 'configure', name=name, secrets={'env': {}}))['ok']
    assert scope.load_server(key).missing == ['TOKEN', 'URL']
    assert scope._secrets(key)['headers'] == {'X-Literal': 'private-header'}


@pytest.mark.asyncio
async def test_shared_per_key_destination_changes_and_invalid_no_write(owned):
    runtime, selected = owned
    key = await add(owned, {'command': 'fixture-server', 'env': {'A': '${TOKEN}', 'B': 'kept-private'}})
    result = await run(runtime, 'add', replace=True, servers=[{'name': key.name, 'server': {
        'command': 'fixture-server', 'env': {'A': 'now-private', 'B': '${OTHER}'},
    }}])
    assert result['ok']
    assert scope._raw_entry(key)['env'] == {'B': '${OTHER}'}
    assert scope._secrets(key)['env'] == {'A': 'now-private'}
    assert (await run(runtime, 'add', replace=True, servers=[{'name': key.name, 'server': {
        'command': 'fixture-server', 'env': {'C': 'extra'},
    }}]))['ok']
    assert scope._raw_entry(key)['env'] == {'B': '${OTHER}'}
    assert scope._secrets(key)['env'] == {'A': 'now-private', 'C': 'extra'}
    paths = [p for p in repository.package_dir(key).rglob('*') if p.is_file()]
    if selected.project_root:
        paths.append(selected.project_root / '.mcp.json')
    before = {p: p.read_bytes() for p in paths}
    invalid = await run(runtime, 'add', replace=True, servers=[{'name': key.name, 'server': {'url': '${URL}', 'headers': 42}}])
    assert not invalid['ok']
    assert not (await run(runtime, 'configure', name=key.name, secrets={'env': None}))['ok']
    assert {p: p.read_bytes() for p in paths} == before


@pytest.mark.asyncio
async def test_mutation_excludes_new_admissions_before_writing_secrets(owned, monkeypatch):
    runtime, _ = owned
    key = await add(owned, {'command': 'fixture-server'})
    entered, release = threading.Event(), threading.Event()
    configure = scope.configure
    calls = []

    def paused(*args):
        entered.set()
        assert release.wait(5)
        configure(*args)

    async def called(*args):
        calls.append(args)
        return SimpleNamespace(model_dump=lambda **_: {'content': [], 'isError': False})

    monkeypatch.setattr(scope, 'configure', paused)
    monkeypatch.setattr(runtime, '_call_once', called)
    task = asyncio.create_task(run(runtime, 'configure', name=key.name, secrets={'env': {'TOKEN': 'new'}}))
    assert await asyncio.to_thread(entered.wait, 5)
    call = asyncio.create_task(run(runtime, 'call', name=key.name, tool='ping'))
    try:
        await asyncio.sleep(0.05)
        assert not calls
        assert not call.done()
    finally:
        release.set()
        await task
        await call
    assert calls[0][3]['transport']['env'] == {'TOKEN': 'new'}


@pytest.mark.asyncio
async def test_rejected_native_deletion_preserves_publication_and_oauth(tmp_path, monkeypatch):
    save_config(tmp_path)
    path = native.claude_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'mcpServers': {'native': {'url': 'https://example.invalid/mcp'}}}))
    runtime = McpRuntime()
    key = scope.ScopedName(scope.Scope(), 'native')
    definition = repository.read_definition(key)
    revision = repository.publish(key, definition=definition, manifest={}, tools=[{'name': 'ping'}], commands=[])
    oauth = repository.package_dir(key) / 'oauth.json'
    oauth.write_text('{"fixture": "keep"}')
    provider = object()
    runtime._oauth_providers[key] = provider

    def refuse(*_):
        raise McpHubError('config_changed', 'fixture refusal')
    monkeypatch.setattr(native, 'remove_entry', refuse)
    result = await run(runtime, 'remove', name='native', **{'global': True})
    assert not result['ok']
    assert repository.read_published(key)['revision'] == revision
    assert oauth.read_text() == '{"fixture": "keep"}'
    assert runtime._oauth_providers[key] is provider
    assert McpRuntime._admit(key, 'ping', {})['revision'] == revision


@pytest.mark.parametrize('transport', [
    {'type': 'stdio', 'command': 'legacy-server', 'args': [], 'env': {'TOKEN': 'legacy-private'}},
    {'type': 'http', 'url': 'https://example.invalid/mcp', 'headers': {'Authorization': 'legacy-private'}},
])
def test_released_normalized_global_files_load_without_rewrite(tmp_path, transport):
    save_config(tmp_path)
    key = scope.ScopedName(scope.Scope(), 'legacy')
    directory = repository.package_dir(key)
    directory.mkdir(parents=True)
    path = directory / 'definition.json'
    released = {'schemaVersion': 1, 'name': 'legacy', 'transport': transport}
    path.write_text(json.dumps(released))
    before = path.read_bytes()
    effective = repository.read_definition(key)
    assert all(effective['transport'][k] == v for k, v in transport.items() if k != 'type')
    row = repository.summarize('legacy')
    assert row['state'] == 'not_generated'
    assert 'legacy-private' not in json.dumps(row)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('owned', ['global'], indirect=True)
@pytest.mark.parametrize('entry', [
    {'url': 'https://example.invalid/mcp', 'headers': {'Authorization': 'Bearer ${TOKEN}', 'X-Mode': 'readonly'}},
    {'command': 'fixture-server', 'env': {'AUTH': 'Bearer ${TOKEN}', 'MODE': 'readonly'}},
])
async def test_identical_mixed_global_import_is_unchanged(owned, entry):
    """Mixed templates/private values must compare at the same boundary that writes them."""
    runtime, _ = owned
    key = await add(owned, entry)
    assert (await run(runtime, 'configure', name=key.name, secrets={'env': {'TOKEN': 'configured-secret', **({'MODE': 'readonly'} if 'command' in entry else {})}}))['ok']
    before = repository.read_published(key)['revision']
    result = await run(runtime, 'add', servers=[{'name': key.name, 'server': entry}])
    assert result['ok'], result
    assert result['unchanged'] == [{'name': key.name, 'status': 'unchanged'}]
    assert repository.read_published(key)['revision'] == before

    literal = json.loads(json.dumps(entry).replace('${TOKEN}', 'configured-secret'))
    changed = await run(runtime, 'add', servers=[{'name': key.name, 'server': literal}])
    assert changed['failed'][0]['type'] == 'exists'
    assert repository.read_published(key)['revision'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('owned', ['global'], indirect=True)
async def test_repeated_released_global_import_keeps_publication(owned):
    """A released file's inline private values are equivalent to the new partition."""
    from core.mcp_hub.definition import definition_from_provider

    runtime, selected = owned
    entry = {'url': 'https://example.invalid/mcp', 'headers': {'X-Mode': 'readonly'}}
    key = scope.ScopedName(selected, 'legacy')
    repository.write_avibe_definition(definition_from_provider(key.name, entry))
    assert (await run(runtime, 'refresh', name=key.name))['ok']
    before = repository.read_published(key)['revision']
    result = await run(runtime, 'add', servers=[{'name': key.name, 'server': entry}])
    assert result['unchanged'] == [{'name': key.name, 'status': 'unchanged'}]
    assert repository.read_published(key)['revision'] == before
