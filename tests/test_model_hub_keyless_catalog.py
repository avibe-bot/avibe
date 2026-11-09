"""Catalog-only keyless addition: consent, identity and wire mapping share one owner.

The two shipped-vendor tests can both pass despite a hidden vendor-name branch.
A third JSON entry must work through the actual V2 loader, chain and transport.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from aiohttp import web

from config.v2_config import ModelHubConfig, V2Config, hosted_tool_setting, model_hub_builtin_source
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.hosted_tools import apply_hosted_tool_put
from core.handlers.model_hub.service import ModelHubService, V2ModelHubConfigStore
from vibe.model_hub_runtime import api_key_vendors
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.keyless_mcp import KeylessMcpClient


@pytest.fixture
def third_vendor(tmp_path, monkeypatch):
    catalog = json.loads(api_key_vendors._catalog_path().read_text())
    entry = json.loads(json.dumps(next(row for row in catalog if row['id'] == 'parallel-mcp')))
    entry.update(id='third-mcp', label='Third (free)', source_id='src_thirdkeyless')
    service = entry['search']
    service.update(service_id='third/search', endpoint='https://third.invalid/mcp')
    service['mcp'].update(tool='find_pages', required_arguments=['phrase', 'count'])
    service['request'] = {'method': 'POST', 'query': 'phrase', 'results': 'count', 'defaults': {}}
    service['result'] = {'results': 'pages', 'url': 'link', 'title': 'heading', 'snippet': 'summary', 'page_age': 'date'}
    service['limits']['results_max'] = 2
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps([*catalog, entry]))
    monkeypatch.setattr(api_key_vendors, '_catalog_path', lambda: path)
    api_key_vendors.api_key_vendor_catalog.cache_clear()
    api_key_vendors._catalog_by_id.cache_clear()
    try:
        yield entry
    finally:
        api_key_vendors.api_key_vendor_catalog.cache_clear()
        api_key_vendors._catalog_by_id.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('structured', [False, True])
async def test_json_only_keyless_vendor_roundtrip_dispatch_and_session_reuse(third_vendor, tmp_path, monkeypatch, structured):
    """MH-SEARCH-SERVICES-001: a stateless MCP vendor needs no Python or UI branch."""
    calls = []

    async def peer(request):
        payload = await request.json()
        calls.append(payload)
        method = payload['method']
        if method == 'notifications/initialized':
            return web.Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'find_pages', 'inputSchema': {'properties': {
                'phrase': {}, 'count': {}}, 'required': ['phrase', 'count']}}]}
        else:
            assert payload['params'] == {'name': 'find_pages', 'arguments': {'phrase': 'catalog only', 'count': 2}}
            content = {'pages': [
                {'link': 'https://example.org/one', 'heading': 'One', 'summary': 'Matched phrase'},
                {'link': 'https://example.org/two'}, {'link': 'https://example.org/three'},
            ]}
            result = {'structuredContent': content} if structured else {'content': [
                {'type': 'text', 'text': '{"request_id":"metadata-before-results"}'},
                {'type': 'text', 'text': json.dumps(content)},
            ]}
        # Stateless Streamable HTTP intentionally omits Mcp-Session-Id.
        return web.json_response({'jsonrpc': '2.0', 'id': payload['id'], 'result': result})

    app = web.Application()
    app.router.add_post('/mcp', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    endpoint = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp'
    config = V2Config.default()
    config.model_hub.enabled = True
    config.save()
    store = V2ModelHubConfigStore()
    adapter = CLIProxyEngineAdapter()
    # Replace only the network destination; identity and protocol metadata are
    # read from the third JSON entry by the real adapter.
    adapter._keyless_clients['third/search'] = KeylessMcpClient(third_vendor['search'], endpoint=endpoint)
    service = ModelHubService(store=store, adapter=adapter, events=BoundedEventLog(tmp_path / 'events.json'))
    sid = 'src_thirdkeyless'
    try:
        loaded = ModelHubConfig.from_payload(store.load().to_payload())
        turn = service.freeze_turn_hosted_tools(loaded)
        assert [slot.source_id for slot in turn.executors['web_search']] == ['src_exakeyless']
        vendor = next(v for t in service.hosted_tools_status()['hosted_tools']['tools'] if t['name'] == 'web_search'
                      for v in t['service_vendors'] if v['id'] == 'third-mcp')
        assert vendor['source_ids'] == [sid] and vendor['facts']['default_enabled'] is False
        assert vendor['endpoint'] == 'https://third.invalid/mcp'
        loaded.hosted_tools['web_search'] = apply_hosted_tool_put(loaded, 'web_search', {
            'services': {sid: {'enabled': True, 'results': 2}, 'src_exakeyless': {'enabled': False}},
            'executor_order': [sid, 'src_exakeyless'],
        })
        store.save(loaded)
        loaded = ModelHubConfig.from_payload(store.load().to_payload())
        assert hosted_tool_setting(loaded, 'web_search').services[sid].to_payload() == {'enabled': True, 'results': 2}
        for _ in range(2):
            result = await service.run_hosted_tool('web_search', {'arguments': {'query': 'catalog only'}},
                                                  turn=service.freeze_turn_hosted_tools(loaded))
            assert result['record']['sources'] == [
                {'url': 'https://example.org/one', 'title': 'One', 'snippet': 'Matched phrase'},
                {'url': 'https://example.org/two'},
            ]
            assert result['execution']['executor_origin']['provider'] == 'third-mcp'
        assert [call['method'] for call in calls] == [
            'initialize', 'notifications/initialized', 'tools/list', 'tools/call', 'tools/call',
        ]
        assert not store.load().sources
        for changed in ({'credential_ref': 'dummy'}, {'vendor': 'exa-mcp'}, {'source_id': 'src_wrong000'}):
            args = dict(source_id=sid, vendor='third-mcp', service_id='third/search', credential_ref=None, request={})
            args.update(changed)
            rejected = await adapter.search(**args)
            assert rejected.error == 'invalid_endpoint'
        # A keyless catalog identity never grants an explicit credential access.
        from config.v2_config import model_hub_search_service
        assert model_hub_search_service(replace(model_hub_builtin_source(sid), credential_ref='dummy')) is None
    finally:
        for client in adapter._keyless_clients.values():
            await client.close()
        await runner.cleanup()
