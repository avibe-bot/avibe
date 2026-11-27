"""A picker may use an observed inventory only for an unambiguous native authority."""
import json

import pytest

from config.v2_config import V2Config
from core.handlers.direct_inventory_identity import inventory_identity
from core.handlers.direct_model_menu import PickerModel, publish_inventory, read_menu


@pytest.fixture
def codex(tmp_path, monkeypatch):
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path / 'avibe'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    for key in ('CODEX_ACCESS_TOKEN', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / 'config.toml').write_text('cli_auth_credentials_store="file"\n')
    (tmp_path / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'fixture'}))
    cfg = V2Config.default()
    cfg.agents.codex.enabled = True
    cfg.model_hub.agents['codex'].mode = 'direct'
    cfg.save()
    key = inventory_identity('codex', cfg)
    assert key
    publish_inventory('codex', key, [PickerModel('fixture-private', reasoning_efforts=('high',))])
    return cfg


@pytest.mark.parametrize('change', ['openai_base_url', 'chatgpt_base_url', 'access-token', 'mixed-mode'])
def test_unknown_codex_authority_never_selects_previous_inventory(codex, tmp_path, monkeypatch, change):
    if change == 'access-token':
        monkeypatch.setenv('CODEX_ACCESS_TOKEN', 'other-fixture')
    elif change == 'mixed-mode':
        (tmp_path / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': 'fixture',
            'tokens': {'account_id': 'other-fixture', 'access_token': 'opaque'}}))
    else:
        with (tmp_path / 'config.toml').open('a') as f:
            print(f'{change}="https://example.invalid"', file=f)
    identity = inventory_identity('codex', codex)
    assert read_menu('codex', identity, fallback=(PickerModel('public'),)).models == (PickerModel('public'),)
    assert identity is None


def test_existing_codex_picker_reads_scoped_ids_and_efforts(codex):
    from vibe import api
    from core.handlers.direct_model_menu import set_removed

    snapshot = api.codex_models(schedule_refresh=False)
    assert 'fixture-private' in snapshot['models']
    assert 'high' in [row['value'] for row in snapshot['reasoning_options']['fixture-private']]
    assert 'fixture-private' in [row['value'] for row in api.agent_model_options('codex', cached_only=True)['models']]
    set_removed('codex', 'fixture-private', True)
    assert 'fixture-private' not in api.codex_models(schedule_refresh=False)['models']


def test_claude_custom_headers_are_not_scoped_by_an_unrelated_key(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture')
    monkeypatch.setenv('ANTHROPIC_CUSTOM_HEADERS', 'Authorization: fixture')
    cfg = V2Config.default()
    cfg.agents.claude.enabled = True
    assert inventory_identity('claude', cfg) is None


@pytest.mark.asyncio
async def test_failed_acp_session_preserves_membership(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from modules.agents import acp
    from modules.agents.antigravity.auth import run_antigravity_connection_probe
    from modules.agents.antigravity.models import read_antigravity_live_models, record_antigravity_live_models
    from tests.helpers.acp_probe_stub import make_probe_client
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path / 'avibe'))
    cfg = V2Config.default()
    settings = cfg.agents.antigravity
    settings.auth_mode, settings.api_key = 'gemini-api-key', 'fixture'
    cfg.save()
    def options(ids):
        return [{'id':'model','options':[{'value': value} for value in ids]}]
    record_antigravity_live_models(settings, {'sessionId':'good','configOptions':options(['A','B'])})
    before = read_antigravity_live_models(settings)
    assert [row['id'] for row in before] == ['A','B']
    client, _ = make_probe_client(options=[])
    monkeypatch.setattr(client, 'session_new', AsyncMock(return_value={'configOptions':options(['A'])}))
    monkeypatch.setattr(acp, 'AcpClient', client)
    result = await run_antigravity_connection_probe(binary='/fake/agy', cwd=str(tmp_path), model='A', config=settings)
    assert result['error'] == 'session_create_failed'
    assert read_antigravity_live_models(settings) == before


def test_oauth_email_alone_is_not_a_claude_organization(tmp_path, monkeypatch):
    from vibe import native_oauth_store as store
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    cfg = V2Config.default()
    cfg.agents.claude.enabled = True
    for org in ('one','two'):
        bag = store.NativeOAuthSnapshot('claude', org, {'claudeAiOauth':{'email':'fixture@example.invalid','organizationUuid':org}}, True)
        monkeypatch.setattr(store, 'read_native_oauth', lambda *args, **kwargs: bag)
        assert inventory_identity('claude', cfg) is None


def test_scope_rejects_oversize_codex_config(codex, tmp_path):
    (tmp_path / 'config.toml').write_bytes(b'#' + b'x' * (3 * 1024 * 1024))
    assert inventory_identity('codex', codex) is None
