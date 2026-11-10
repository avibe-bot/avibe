"""MH-SEARCH-SERVICES-001: optional Parallel dispatch through shared MCP.

Synthetic HTTP responses protect consent, bounded excerpts, header coverage and
cleanup. Existing Exa fixtures cannot catch a Parallel schema or dispatch error.
"""
import asyncio
import gzip
import json
from dataclasses import replace

from aiohttp import web
import pytest

from config.v2_config import (
    ModelHubConfig, ModelHubSourceStateConfig, V2Config, hosted_tool_setting, model_hub_builtin_source,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.hosted_tools import apply_hosted_tool_put
from core.handlers.model_hub.service import ModelHubService, V2ModelHubConfigStore
from vibe import __version__
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime import api_key_vendors
from vibe.model_hub_runtime.keyless_mcp import KeylessMcpClient
from core.handlers.model_hub.search_services import search_request

PARALLEL_SOURCE_ID = "src_parallelkeyless"


def _endpoint(monkeypatch, endpoint):
    original = api_key_vendors.api_key_vendor_entry
    vendor = original("parallel-mcp")
    monkeypatch.setattr(api_key_vendors, "api_key_vendor_entry", lambda key:
                        replace(vendor, search={**vendor.search, "endpoint": endpoint})
                        if key == "parallel-mcp" else original(key))


async def _search_parallel(request):
    service = api_key_vendors.search_service("parallel-mcp", "search")
    client = KeylessMcpClient(service)
    try:
        return await client.search(search_request(service, request["objective"], 5))
    finally:
        await client.close()


async def _close_clients(adapter):
    for client in adapter._keyless_clients.values():
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('line_ending', [None, '\r\n', '\n', '\r'], ids=['json', 'sse-crlf', 'sse-lf', 'sse-cr'])
@pytest.mark.parametrize('failed', ['success', 'tool', 'rpc'])
async def test_parallel_opt_in_dispatch_headers_and_teardown(tmp_path, monkeypatch, failed, line_ending):
    """MH-SEARCH-SERVICES-001: saved opt-in uses the registered service and preserves consent."""
    held_open = line_ending is not None
    calls = []
    closed_before_eof = asyncio.Event()

    async def peer(request):
        payload = await request.json() if request.method == 'POST' else {}
        calls.append((request.method, payload.get('method'), dict(request.headers)))
        if request.method == 'GET':
            return web.Response(status=405)
        if request.method == 'DELETE':
            return web.Response(status=503)  # Remote cleanup cannot erase the outcome.
        method = payload['method']
        if method == 'notifications/initialized':
            return web.Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': payload['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'synthetic', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'web_search', 'inputSchema': {'type': 'object', 'properties': {
                'objective': {'type': 'string'}, 'search_queries': {'type': 'array', 'items': {'type': 'string'}}}}}]}
        else:
            assert method == 'tools/call'
            assert payload['params']['name'] == 'web_search'
            assert payload['params']['arguments'] == {'objective': 'test site:python.org asyncio', 'search_queries': ['test site:python.org asyncio']}
            result = {'content': [{'type': 'text', 'text': 'rate limit exceeded'}], 'isError': True} if failed == 'tool' else {
                'content': [], 'structuredContent': {'results': [{'url': 'https://example.org/synthetic',
                 'title': 'Synthetic', 'excerpts': ['alpha\u0085beta\u2028gamma\u2029delta'], 'publish_date': '2026-11-06'},
                 {'url': 'https://example.org/extra', 'excerpts': ['clamped by the saved result limit']}]}}
        if method == 'tools/call' and held_open:
            reply = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
            if failed == 'rpc':
                reply.pop('result')
                reply['error'] = {'code': -32042, 'message': 'synthetic failure'}
            stream = web.StreamResponse(headers={'Content-Type': 'Text/Event-Stream'})
            await stream.prepare(request)
            notice = {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
            for message in (notice, reply):
                await stream.write((line_ending.join([
                    'event: message', 'data: ' + json.dumps(message, ensure_ascii=False), '', '',
                ])).encode())
            # Hold the SSE response open after its matching result/error: the
            # shared client must finish without waiting for EOF.
            for _ in range(800):
                if request.transport is None or request.transport.is_closing():
                    closed_before_eof.set()
                    break
                await asyncio.sleep(.01)
            return stream
        if method == 'tools/call' and failed == 'rpc':
            return web.json_response({'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32042, 'message': 'synthetic failure'}})
        return web.json_response({'jsonrpc': '2.0', 'id': payload['id'], 'result': result}, headers={'Mcp-Session-Id': 'synthetic-session'})

    app = web.Application()
    app.router.add_route('*', '/mcp', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    _endpoint(monkeypatch, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp')
    config = V2Config.default()
    config.model_hub.enabled = True
    config.save()
    store = V2ModelHubConfigStore()
    adapter = CLIProxyEngineAdapter()
    service = ModelHubService(store=store, adapter=adapter, events=BoundedEventLog(tmp_path/'events.json'))
    try:
        before = ModelHubConfig.from_payload(store.load().to_payload())
        turn = service.freeze_turn_hosted_tools(before)
        assert [s.source_id for s in turn.executors['web_search']] == ['src_exakeyless']
        assert not calls
        before.hosted_tools['web_search'] = apply_hosted_tool_put(before, 'web_search', {
            'services': {PARALLEL_SOURCE_ID: {'enabled': True, 'results': 1}, 'src_exakeyless': {'enabled': False}},
            'executor_order': [PARALLEL_SOURCE_ID, 'src_exakeyless'],
        })
        store.save(before)
        loaded = ModelHubConfig.from_payload(store.load().to_payload())  # Actual offline config loader retains settings and role order.
        assert hosted_tool_setting(loaded, 'web_search').services[PARALLEL_SOURCE_ID].enabled is True
        assert hosted_tool_setting(loaded, 'web_search').executor_order == (PARALLEL_SOURCE_ID, 'src_exakeyless')
        result = await asyncio.wait_for(service.run_hosted_tool('web_search', {'arguments': {'query': 'test site:python.org asyncio'}},
                                               turn=service.freeze_turn_hosted_tools(loaded)), 3)
        if held_open:
            await asyncio.wait_for(closed_before_eof.wait(), 1)
        if failed != 'success':
            assert result['record']['error_code'] == ('service_rate_limited' if failed == 'tool' else 'service_unavailable')
        else:
            assert result['record']['status'] == 'completed'
            assert len(result['record']['sources']) == 1
            assert result['record']['sources'][0]['snippet'] == 'alpha\u0085beta\u2028gamma\u2029delta'
            assert result['execution']['executor_origin']['provider'] == 'parallel-mcp'
        assert {c[:2] for c in calls} >= {('POST', 'initialize'), ('POST', 'notifications/initialized'),
                                        ('POST', 'tools/list'), ('POST', 'tools/call')}
        assert all(h.get('User-Agent') == f'Avibe/{__version__}' for _, _, h in calls)
        assert all('Authorization' not in h and 'x-api-key' not in h for _, _, h in calls)
        tool_calls = sum(method == 'tools/call' for _, method, _ in calls)

        async def refuse():
            raise RuntimeError('synthetic final admission refused')

        refused = await adapter.search(PARALLEL_SOURCE_ID, 'parallel-mcp', 'parallel/mcp', None,
                                       {'objective': 'refused', 'search_queries': ['refused']}, on_sent=refuse)
        assert refused.on_sent_fired is False
        assert sum(method == 'tools/call' for _, method, _ in calls) == tool_calls
        count = len(calls)
        rejected = await adapter.search(PARALLEL_SOURCE_ID, 'parallel-mcp', 'parallel/mcp', 'dummy-explicit-key', {}, on_sent=None)
        assert rejected.error == 'invalid_endpoint' and len(calls) == count
        loaded.hosted_tools['web_search'] = apply_hosted_tool_put(loaded, 'web_search', {'services': {PARALLEL_SOURCE_ID: {'enabled': False}}})
        store.save(loaded)
        frozen = service.freeze_turn_hosted_tools(store.load())
        assert not any(s.enabled for s in frozen.executors['web_search'])
        loaded.hosted_tools['web_search'] = replace(hosted_tool_setting(loaded, 'web_search'), enabled=False)
        store.save(loaded)
        assert 'web_search' not in service.freeze_turn_hosted_tools(store.load()).tools
        assert len(calls) == count
    finally:
        await _close_clients(adapter)
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('after_freeze', [False, True])
@pytest.mark.parametrize('changes', [
    {'credential_ref': 'dummy-explicit-ref'},
    {'credential_ref': 'dummy-explicit-ref', 'state': ModelHubSourceStateConfig(status='needs_action', detail_key='models.source.needs_action.credential_revoked')},
    {'credential_ref': 'dummy-explicit-ref', 'protocol': 'openai_chat', 'base_url': 'https://example.invalid/retargeted'},
    {'credential_ref': 'dummy-explicit-ref', 'vendor': 'tavily'},
])
async def test_parallel_persisted_collision_blocks_registered_dispatch(tmp_path, monkeypatch, changes, after_freeze):
    """MH-SEARCH-SERVICES-001: a builtin must not erase persisted identity or blocked state.

    An adapter-only credential test misses the synthetic identity shadowing the
    real loader's Source, including a collision added after a turn was frozen.
    """
    config = V2Config.default()
    config.model_hub.enabled = True
    config.save()
    store = V2ModelHubConfigStore()
    loaded = ModelHubConfig.from_payload(store.load().to_payload())
    loaded.hosted_tools['web_search'] = apply_hosted_tool_put(loaded, 'web_search', {
        'services': {PARALLEL_SOURCE_ID: {'enabled': True}, 'src_exakeyless': {'enabled': False}},
        'executor_order': [PARALLEL_SOURCE_ID, 'src_exakeyless'],
    })
    store.save(loaded)
    adapter = CLIProxyEngineAdapter()
    service = ModelHubService(store=store, adapter=adapter, events=BoundedEventLog(tmp_path/'events.json'))
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError('persisted collision reached service dispatch')

    monkeypatch.setattr(adapter, 'search', spy)
    turn = service.freeze_turn_hosted_tools(store.load()) if after_freeze else None
    loaded.sources.append(replace(model_hub_builtin_source(PARALLEL_SOURCE_ID), **changes))
    store.save(loaded)
    snapshot = store.load().to_payload()
    try:
        result = await service.run_hosted_tool('web_search', {'arguments': {'query': 'synthetic collision'}},
                                               turn=turn or service.freeze_turn_hosted_tools(store.load()))
        assert result['record']['status'] == 'failed'
        assert not calls
        assert store.load().to_payload() == snapshot
    finally:
        await _close_clients(adapter)


@pytest.mark.asyncio
async def test_parallel_ignores_environment_https_proxy(monkeypatch):
    """MH-SEARCH-SERVICES-001: real shared-client egress ignores HTTPS_PROXY.

    A local TLS sink closes before HTTP; no query/key is sent. This catches proxy
    routing at the socket boundary, which client configuration assertions miss.
    """
    proxy_lines, direct_handshakes = [], []

    async def proxy(reader, writer):
        try:
            data = await reader.readuntil(b'\r\n\r\n')
            proxy_lines.append(data.split(b'\r\n', 1)[0])
            writer.write(b'HTTP/1.1 502 Synthetic Proxy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def direct(reader, writer):
        try:
            data = await reader.read(5)
            direct_handshakes.append(bool(data and data[0] == 22))
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(proxy, '127.0.0.1', 0) as proxy_server, \
            await asyncio.start_server(direct, '127.0.0.1', 0) as target:
        for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
            monkeypatch.setenv(name, f'http://127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}')
        for name in ('NO_PROXY', 'no_proxy'):
            monkeypatch.setenv(name, '')
        _endpoint(monkeypatch, f'https://127.0.0.1:{target.sockets[0].getsockname()[1]}/mcp')
        adapter = CLIProxyEngineAdapter()
        try:
            result = await asyncio.wait_for(adapter.search(PARALLEL_SOURCE_ID, 'parallel-mcp', 'parallel/mcp', None,
                                                          {'objective': 'synthetic'}, on_sent=None), 3)
            assert result.error == 'connection' and result.on_sent_fired is False
            assert direct_handshakes == [True]
            assert proxy_lines == []
        finally:
            await _close_clients(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['redirect', 'oversize'])
async def test_parallel_rejects_redirects_and_oversized_responses(monkeypatch, boundary):
    """MH-SEARCH-SERVICES-001: MCP must bound decoded bodies and refuse redirects.

    A small valid result can hide a large HTTP body. A same-origin redirect
    still leaves the configured endpoint, including during session teardown.
    """
    received = []

    async def peer(request):
        message = await request.json() if request.method == 'POST' else {}
        received.append((request.path, message.get('method') or request.method))
        if request.path == '/other':
            return web.Response(status=204)
        if boundary == 'redirect':
            return web.Response(status=307, headers={'Location': '/other'})
        if message.get('method') != 'initialize':
            return web.Response(status=503)
        reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': {
            'protocolVersion': message['params']['protocolVersion'],
            'capabilities': {'tools': {}}, 'serverInfo': {'name': 'small', 'version': '1'},
        }}
        if boundary == 'redirect':
            return web.json_response(reply, headers={'Mcp-Session-Id': 'synthetic-session'})
        # Count decoded bytes, before the SDK's JSON parser discards whitespace.
        body = gzip.compress(b' ' * (8 * 1024 * 1024) + json.dumps(reply).encode())
        return web.Response(body=body, content_type='application/json', headers={'Content-Encoding': 'gzip'})

    app = web.Application()
    app.router.add_route('*', '/{path}', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        _endpoint(monkeypatch, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp')
        answer = await asyncio.wait_for(_search_parallel({'objective': 'private query'}), 3)
    finally:
        await runner.cleanup()
    if boundary == 'redirect':
        assert answer.http_status == 307 and answer.error == 'redirect'
        assert received == [('/mcp', 'initialize')]
        assert all(path == '/mcp' for path, _ in received), received
    else:
        assert received == [('/mcp', 'initialize')]
    assert not answer.on_sent_fired


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['disconnect', 'limited', 'json', 'gzip_sse', 'oversize_json', 'oversize_sse'])
async def test_parallel_search_exchange_owns_retry_and_size(monkeypatch, case):
    """MH-SEARCH-SERVICES-001: only the search exchange owns its status, retry hints and wire budget."""
    from config.v2_config import model_hub_search_service
    from core.handlers.model_hub.search_services import map_response

    async def peer(request):
        if request.method != 'POST':
            return web.Response(status=503, headers={'Retry-After': '999'})
        message = await request.json()
        method = message['method']
        if method == 'notifications/initialized':
            return web.Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'test', 'version': '1'}}
            headers = {'Mcp-Session-Id': 'test'}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'web_search', 'inputSchema': {'type': 'object', 'properties': {
                'objective': {}, 'search_queries': {}}}}]}
            headers = {'Retry-After': '630'}
        else:
            assert method == 'tools/call'
            if case == 'disconnect':
                request.transport.abort()  # No response headers were sent for this exchange.
                return web.Response()
            if case == 'limited':
                return web.Response(status=429, headers={'Retry-After': '41'})
            # A structured result and its text fallback are both valid MCP output.
            # The mapper always prefers the structured result and bounds its displayed excerpt.
            row = {'url': 'https://example.org/guide', 'excerpts': ['x' * (1200 * 1024)]}
            structured = {'results': [row]}
            result = {'content': [{'type': 'text', 'text': json.dumps(structured)}], 'structuredContent': structured}
            wire = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            if 'sse' in case:
                wire = b'event: message\ndata: ' + wire + b'\n\n'
                if case == 'oversize_sse':
                    wire = (b':' + b' ' * 8192 + b'\n\n') * 1024 + wire
                return web.Response(body=gzip.compress(wire), content_type='text/event-stream',
                                    headers={'Content-Encoding': 'gzip'})
            if case == 'oversize_json':
                wire = b' ' * (8 * 1024 * 1024) + wire
            return web.Response(body=wire, content_type='application/json')
        return web.json_response({'jsonrpc': '2.0', 'id': message['id'], 'result': result}, headers=headers)

    app = web.Application()
    app.router.add_route('*', '/mcp', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        _endpoint(monkeypatch, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp')
        answer = await asyncio.wait_for(_search_parallel({'objective': 'test'}), 5)
    finally:
        await runner.cleanup()

    service = model_hub_search_service(model_hub_builtin_source(PARALLEL_SOURCE_ID))
    record = map_response(service, answer, sent=answer.on_sent_fired, results=1, say=lambda key: key).record
    assert answer.on_sent_fired
    headers = {k.lower(): v for k, v in answer.headers.items()}
    if case == 'disconnect':
        assert (answer.http_status, answer.response_received_at) == (None, None)
        assert 'retry-after' not in headers
        assert record['error_code'] == 'service_unavailable'
    elif case == 'limited':
        assert answer.http_status == 429 and headers['retry-after'] == '41'
        assert record['error_code'] == 'service_rate_limited'
    elif case.startswith('oversize'):
        assert record['status'] == 'failed'
    else:
        assert answer.http_status == 200 and 'retry-after' not in headers
        assert record['sources'] == [{'url': 'https://example.org/guide', 'snippet': 'x' * 1000}]
