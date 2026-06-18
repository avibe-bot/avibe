from modules.agents.opencode.config_reconciler import OpenCodeConfigReconciler


def _reconcile(user_config, live_config=None, auth_entries=None):
    return OpenCodeConfigReconciler().reconcile(
        user_config=user_config,
        live_config=live_config or {},
        auth_entries=auth_entries or {},
    )


def test_user_config_is_top_level_source_of_truth():
    assert _reconcile(
        {"provider": {"deepseek": {"options": {"baseURL": "https://api.deepseek.com"}}}},
        {
            "permission": "allow",
            "model": "openai/gpt-5",
            "provider": {"deepseek": {"options": {"baseURL": "https://old.example"}}},
        },
    ) == {
        "provider": {"deepseek": {"options": {"baseURL": "https://api.deepseek.com"}}}
    }


def test_user_options_do_not_resurrect_live_deleted_keys():
    assert _reconcile(
        {"provider": {"openai": {"options": {"apiKey": "sk-config"}}}},
        {
            "provider": {
                "openai": {
                    "options": {
                        "apiKey": "sk-config",
                        "baseURL": "https://deleted.example/v1",
                    }
                }
            }
        },
    )["provider"]["openai"]["options"] == {"apiKey": "sk-config"}


def test_missing_user_options_drops_live_options_without_auth():
    provider = _reconcile(
        {"provider": {"deepseek": {"models": {"deepseek-v4": {"id": "deepseek-v4"}}}}},
        {
            "provider": {
                "deepseek": {
                    "options": {
                        "apiKey": "sk-stale",
                        "baseURL": "https://stale.example",
                    },
                    "models": {"deepseek-v4": {"id": "deepseek-v4"}},
                }
            }
        },
    )["provider"]["deepseek"]

    assert "options" not in provider
    assert provider["models"] == {"deepseek-v4": {"id": "deepseek-v4"}}


def test_missing_user_options_preserves_auth_json_api_key():
    provider = _reconcile(
        {"provider": {"deepseek": {"models": {"deepseek-v4": {"id": "deepseek-v4"}}}}},
        {
            "provider": {
                "deepseek": {
                    "options": {
                        "apiKey": "sk-stale-live",
                        "baseURL": "https://stale.example",
                    },
                    "models": {"deepseek-v4": {"id": "deepseek-v4"}},
                }
            }
        },
        {"deepseek": {"type": "api", "key": "sk-auth-json"}},
    )["provider"]["deepseek"]

    assert provider["options"] == {"apiKey": "sk-auth-json"}
    assert provider["models"] == {"deepseek-v4": {"id": "deepseek-v4"}}


def test_auth_json_key_overrides_stale_live_key_when_user_has_options():
    assert _reconcile(
        {"provider": {"openai": {"options": {"baseURL": "https://relay.example/v1"}}}},
        {
            "provider": {
                "openai": {
                    "options": {
                        "apiKey": "sk-old-live",
                        "baseURL": "https://old.example/v1",
                    }
                }
            }
        },
        {"openai": {"type": "api", "key": "sk-new-auth"}},
    )["provider"]["openai"]["options"] == {
        "baseURL": "https://relay.example/v1",
        "apiKey": "sk-new-auth",
    }


def test_oauth_user_provider_does_not_reintroduce_stale_api_key():
    assert _reconcile(
        {"provider": {"openai": {"options": {"baseURL": "https://relay.example/v1"}}}},
        {
            "provider": {
                "openai": {
                    "options": {
                        "apiKey": "sk-stale",
                        "baseURL": "https://old.example/v1",
                    }
                }
            }
        },
        {"openai": {"type": "oauth", "refresh": "oauth-refresh"}},
    )["provider"]["openai"]["options"] == {"baseURL": "https://relay.example/v1"}


def test_user_models_do_not_resurrect_live_deleted_models():
    assert _reconcile(
        {
            "provider": {
                "deepseek": {
                    "options": {"baseURL": "https://api.deepseek.com"},
                    "models": {"keep-model": {"id": "keep-model"}},
                }
            }
        },
        {
            "provider": {
                "deepseek": {
                    "options": {"baseURL": "https://api.deepseek.com"},
                    "models": {
                        "keep-model": {"id": "keep-model"},
                        "deleted-model": {"id": "deleted-model"},
                    },
                }
            }
        },
    )["provider"]["deepseek"]["models"] == {"keep-model": {"id": "keep-model"}}


def test_empty_user_models_block_drops_live_models():
    assert _reconcile(
        {
            "provider": {
                "deepseek": {
                    "options": {"baseURL": "https://api.deepseek.com"},
                    "models": {},
                }
            }
        },
        {
            "provider": {
                "deepseek": {
                    "options": {"baseURL": "https://api.deepseek.com"},
                    "models": {
                        "deleted-model": {"id": "deleted-model"},
                    },
                }
            }
        },
    )["provider"]["deepseek"]["models"] == {}


def test_provider_absent_from_user_config_is_dropped_unless_auth_or_local():
    assert set(
        _reconcile(
            {"provider": {"anthropic": {"options": {"baseURL": "https://relay.example/v1"}}}},
            {
                "provider": {
                    "anthropic": {"options": {"baseURL": "https://relay.example/v1"}},
                    "openai": {"options": {"apiKey": "sk-stale"}},
                }
            },
        )["provider"].keys()
    ) == {"anthropic"}


def test_auth_backed_provider_absent_from_user_config_is_preserved():
    assert set(
        _reconcile(
            {"provider": {"claude-relay": {"options": {"baseURL": "https://relay.example/v1"}}}},
            {
                "provider": {
                    "openai": {
                        "options": {
                            "apiKey": "sk-live",
                            "baseURL": "https://api.openai.com/v1",
                        }
                    },
                    "claude-relay": {"options": {"baseURL": "https://old.example/v1"}},
                }
            },
            {"openai": {"type": "oauth", "refresh": "oauth-refresh"}},
        )["provider"].keys()
    ) == {"claude-relay", "openai"}


def test_auth_backed_provider_absent_from_user_config_uses_auth_json_key():
    openai = _reconcile(
        {"provider": {"claude-relay": {"options": {"baseURL": "https://relay.example/v1"}}}},
        {
            "provider": {
                "openai": {
                    "options": {
                        "apiKey": "sk-old-live",
                        "baseURL": "https://api.openai.com/v1",
                    }
                }
            }
        },
        {"openai": {"type": "api", "key": "sk-new-auth"}},
    )["provider"]["openai"]

    assert openai["options"] == {
        "apiKey": "sk-new-auth",
        "baseURL": "https://api.openai.com/v1",
    }


def test_auth_api_provider_missing_from_live_snapshot_is_preserved_from_auth_json():
    assert _reconcile(
        {"provider": {"claude-relay": {"options": {"baseURL": "https://relay.example/v1"}}}},
        {"provider": {"claude-relay": {"options": {"baseURL": "https://old.example/v1"}}}},
        {"openai": {"type": "api", "key": "sk-auth-json"}},
    )["provider"]["openai"] == {"options": {"apiKey": "sk-auth-json"}}


def test_local_provider_absent_from_user_config_is_preserved():
    assert set(
        _reconcile(
            {"provider": {"claude-relay": {"options": {"baseURL": "https://relay.example/v1"}}}},
            {
                "provider": {
                    "ollama": {
                        "options": {"baseURL": "http://localhost:11434/v1"},
                        "models": {"llama3.1": {"id": "llama3.1"}},
                    },
                    "claude-relay": {"options": {"baseURL": "https://old.example/v1"}},
                }
            },
        )["provider"].keys()
    ) == {"claude-relay", "ollama"}


def test_local_provider_models_config_preserves_live_options():
    ollama = _reconcile(
        {"provider": {"ollama": {"models": {"custom-local": {"id": "custom-local"}}}}},
        {
            "provider": {
                "ollama": {
                    "options": {"baseURL": "http://localhost:11434/v1"},
                    "models": {"llama3.1": {"id": "llama3.1"}},
                }
            }
        },
    )["provider"]["ollama"]

    assert ollama["options"] == {"baseURL": "http://localhost:11434/v1"}
    assert ollama["models"]["custom-local"] == {"id": "custom-local"}


def test_local_provider_user_models_are_added_to_live_models():
    ollama = _reconcile(
        {"provider": {"ollama": {"models": {"custom-local": {"id": "custom-local"}}}}},
        {
            "provider": {
                "ollama": {
                    "options": {"baseURL": "http://localhost:11434/v1"},
                    "models": {
                        "llama3.1": {"id": "llama3.1"},
                        "qwen3": {"id": "qwen3"},
                    },
                }
            }
        },
    )["provider"]["ollama"]

    assert ollama["models"] == {
        "llama3.1": {"id": "llama3.1"},
        "qwen3": {"id": "qwen3"},
        "custom-local": {"id": "custom-local"},
    }


def test_local_provider_deleted_user_model_is_not_restored_from_live_models():
    ollama = _reconcile(
        {"provider": {"ollama": {"models": {"kept-user-model": {"id": "kept-user-model"}}}}},
        {
            "provider": {
                "ollama": {
                    "options": {"baseURL": "http://localhost:11434/v1"},
                    "models": {
                        "runtime-model": {"id": "runtime-model"},
                        "deleted-user-model": {
                            "id": "deleted-user-model",
                            "name": "deleted-user-model",
                            "vibe_remote": {"user_model": True},
                        },
                    },
                }
            }
        },
    )["provider"]["ollama"]

    assert ollama["models"] == {
        "runtime-model": {"id": "runtime-model"},
        "kept-user-model": {"id": "kept-user-model"},
    }


def test_local_provider_unmarked_runtime_model_with_matching_name_is_preserved():
    ollama = _reconcile(
        {"provider": {"ollama": {"models": {"custom-local": {"id": "custom-local"}}}}},
        {
            "provider": {
                "ollama": {
                    "options": {"baseURL": "http://localhost:11434/v1"},
                    "models": {
                        "runtime-model": {"id": "runtime-model", "name": "runtime-model"},
                    },
                }
            }
        },
    )["provider"]["ollama"]

    assert ollama["models"] == {
        "runtime-model": {"id": "runtime-model", "name": "runtime-model"},
        "custom-local": {"id": "custom-local"},
    }


def test_no_user_provider_section_preserves_only_auth_and_local_runtime_providers():
    assert set(
        _reconcile(
            {"model": "openai/gpt-5"},
            {
                "provider": {
                    "openai": {"options": {"apiKey": "sk-old-live"}},
                    "ollama": {"options": {"baseURL": "http://localhost:11434/v1"}},
                    "deleted-custom": {"options": {"apiKey": "sk-deleted"}},
                }
            },
            {"openai": {"type": "api", "key": "sk-new-auth"}},
        )["provider"].keys()
    ) == {"openai", "ollama"}


def test_local_provider_absent_from_user_config_drops_stale_user_models():
    ollama = _reconcile(
        {"model": "openai/gpt-5"},
        {
            "provider": {
                "ollama": {
                    "options": {"baseURL": "http://localhost:11434/v1"},
                    "models": {
                        "runtime-model": {"id": "runtime-model", "name": "runtime-model"},
                        "deleted-user-model": {
                            "id": "deleted-user-model",
                            "name": "deleted-user-model",
                            "vibe_remote": {"user_model": True},
                        },
                    },
                }
            }
        },
    )["provider"]["ollama"]

    assert ollama["models"] == {
        "runtime-model": {"id": "runtime-model", "name": "runtime-model"},
    }


def test_no_user_provider_section_preserves_auth_api_provider_missing_from_live_snapshot():
    assert _reconcile(
        {"model": "openai/gpt-5"},
        {"provider": {"ollama": {"options": {"baseURL": "http://localhost:11434/v1"}}}},
        {"openai": {"type": "api", "key": "sk-auth-json"}},
    )["provider"]["openai"] == {"options": {"apiKey": "sk-auth-json"}}


def test_explicit_non_object_user_provider_removes_provider_payload():
    assert "provider" not in _reconcile(
        {"provider": None},
        {"provider": {"openai": {"options": {"apiKey": "sk-live"}}}},
        {"openai": {"type": "api", "key": "sk-auth"}},
    )
