"""Helpers for reading OpenCode user config files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_VALID_REASONING_VARIANTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_CUSTOM_PROVIDER_META_KEY = "vibe_remote"
_CUSTOM_PROVIDER_ADAPTERS = {
    "openai-compatible": "@ai-sdk/openai-compatible",
    "anthropic-compatible": "@ai-sdk/anthropic",
}
_CUSTOM_PROVIDER_LABELS = {
    "openai-compatible": "OpenAI compatible",
    "anthropic-compatible": "Anthropic compatible",
}
_RESERVED_PROVIDER_IDS = {
    "alibaba-cn",
    "anthropic",
    "deepseek",
    "github-copilot",
    "google",
    "groq",
    "lm-studio",
    "lmstudio",
    "minimax",
    "mistral",
    "moonshot",
    "ollama",
    "openai",
    "openrouter",
    "poe",
    "together",
    "vercel",
    "xai",
}


@dataclass(slots=True)
class OpenCodeConfigProbeResult:
    config: Optional[Dict[str, Any]] = None
    content: Optional[str] = None
    path: Optional[Path] = None
    existing_paths: list[Path] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def get_opencode_config_paths(home: Path | None = None) -> list[Path]:
    resolved_home = home or Path.home()
    return [
        resolved_home / ".config" / "opencode" / "opencode.json",
        resolved_home / ".opencode" / "opencode.json",
    ]


def _strip_jsonc_comments(source: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False

    i = 0
    while i < len(source):
        char = source[i]
        next_char = source[i + 1] if i + 1 < len(source) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                result.append(char)
            i += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                i += 2
                continue
            if char == "\n":
                result.append(char)
            i += 1
            continue

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue

        if char == "/" and next_char == "/":
            in_line_comment = True
            i += 2
            continue

        if char == "/" and next_char == "*":
            in_block_comment = True
            i += 2
            continue

        result.append(char)
        i += 1

    return "".join(result)


def _strip_trailing_commas(source: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False

    i = 0
    while i < len(source):
        char = source[i]

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue

        if char == ",":
            j = i + 1
            while j < len(source) and source[j] in " \t\r\n":
                j += 1
            if j < len(source) and source[j] in "}]":
                i += 1
                continue

        result.append(char)
        i += 1

    return "".join(result)


def parse_jsonc_object(content: str) -> Dict[str, Any]:
    normalized = _strip_trailing_commas(_strip_jsonc_comments(content.lstrip("\ufeff"))).strip()
    if not normalized:
        raise ValueError("empty JSONC content")

    parsed = json.loads(normalized)
    if not isinstance(parsed, dict):
        raise ValueError("root is not a JSON object")
    return parsed


@dataclass(slots=True)
class _JsoncTopLevelProperty:
    key: str
    key_start: int
    value_start: int
    value_end: int
    delimiter_index: int
    delimiter: str


def _consume_json_string(source: str, start: int) -> int:
    i = start + 1
    escaped = False
    while i < len(source):
        char = source[i]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _consume_jsonc_comment(source: str, start: int) -> int:
    next_char = source[start + 1] if start + 1 < len(source) else ""
    if next_char == "/":
        i = start + 2
        while i < len(source) and source[i] != "\n":
            i += 1
        return i
    if next_char == "*":
        i = start + 2
        while i + 1 < len(source):
            if source[i] == "*" and source[i + 1] == "/":
                return i + 2
            i += 1
        raise ValueError("unterminated block comment")
    raise ValueError("expected JSONC comment")


def _skip_jsonc_whitespace_and_comments(source: str, start: int) -> int:
    i = start
    while i < len(source):
        char = source[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char == "/" and i + 1 < len(source) and source[i + 1] in "/*":
            i = _consume_jsonc_comment(source, i)
            continue
        return i
    return i


def _find_matching_jsonc_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    i = start
    while i < len(source):
        char = source[i]
        if char == '"':
            i = _consume_json_string(source, i)
            continue
        if char == "/" and i + 1 < len(source) and source[i + 1] in "/*":
            i = _consume_jsonc_comment(source, i)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unterminated {opening}{closing} structure")


def _consume_jsonc_primitive(source: str, start: int) -> int:
    i = start
    while i < len(source):
        char = source[i]
        if char in " \t\r\n,}]":
            return i
        if char == "/" and i + 1 < len(source) and source[i + 1] in "/*":
            return i
        i += 1
    return i


def _consume_jsonc_value(source: str, start: int) -> int:
    if start >= len(source):
        raise ValueError("missing JSON value")

    char = source[start]
    if char == '"':
        return _consume_json_string(source, start)
    if char == "{":
        return _find_matching_jsonc_delimiter(source, start, "{", "}") + 1
    if char == "[":
        return _find_matching_jsonc_delimiter(source, start, "[", "]") + 1
    return _consume_jsonc_primitive(source, start)


def _line_start(source: str, index: int) -> int:
    return source.rfind("\n", 0, index) + 1


def _detect_newline(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def _indent_slice(source: str, line_start: int, token_start: int) -> str:
    return source[line_start:token_start].replace("\ufeff", "")


def _scan_jsonc_top_level_properties(source: str) -> tuple[int, int, list[_JsoncTopLevelProperty]]:
    root_start = 1 if source.startswith("\ufeff") else 0
    root_start = _skip_jsonc_whitespace_and_comments(source, root_start)
    if root_start >= len(source) or source[root_start] != "{":
        raise ValueError("root is not a JSON object")

    root_end = _find_matching_jsonc_delimiter(source, root_start, "{", "}")
    properties: list[_JsoncTopLevelProperty] = []

    i = root_start + 1
    while True:
        i = _skip_jsonc_whitespace_and_comments(source, i)
        if i >= root_end:
            break
        if source[i] != '"':
            raise ValueError("expected object property")

        key_end = _consume_json_string(source, i)
        key = json.loads(source[i:key_end])

        colon_index = _skip_jsonc_whitespace_and_comments(source, key_end)
        if colon_index >= len(source) or source[colon_index] != ":":
            raise ValueError("expected ':' after object property name")

        value_start = _skip_jsonc_whitespace_and_comments(source, colon_index + 1)
        value_end = _consume_jsonc_value(source, value_start)
        delimiter_index = _skip_jsonc_whitespace_and_comments(source, value_end)
        delimiter = source[delimiter_index] if delimiter_index < len(source) else ""

        if delimiter not in {",", "}"}:
            raise ValueError("expected ',' or '}' after object property")

        properties.append(
            _JsoncTopLevelProperty(
                key=key,
                key_start=i,
                value_start=value_start,
                value_end=value_end,
                delimiter_index=delimiter_index,
                delimiter=delimiter,
            )
        )

        if delimiter == "}":
            break
        i = delimiter_index + 1

    return root_start, root_end, properties


def set_jsonc_top_level_string_property(source: str, key: str, value: str) -> str:
    parse_jsonc_object(source)

    root_start, root_end, properties = _scan_jsonc_top_level_properties(source)
    serialized_value = json.dumps(value)

    matching_property = next((prop for prop in reversed(properties) if prop.key == key), None)
    if matching_property is not None:
        return (
            source[: matching_property.value_start]
            + serialized_value
            + source[matching_property.value_end :]
        )

    newline = _detect_newline(source)
    root_line_start = _line_start(source, root_start)
    closing_line_start = _line_start(source, root_end)
    root_indent = _indent_slice(source, root_line_start, root_start)
    first_property_indent = None
    if properties:
        first_property = properties[0]
        first_property_line_start = _line_start(source, first_property.key_start)
        candidate_indent = _indent_slice(source, first_property_line_start, first_property.key_start)
        if candidate_indent.strip() == "":
            first_property_indent = candidate_indent

    child_indent = first_property_indent or (root_indent + "  ")
    property_text = f'{json.dumps(key)}: {serialized_value}'
    has_multiline_layout = "\n" in source[root_start:root_end]
    closing_brace_on_own_line = source[closing_line_start:root_end].strip() == ""

    if not properties:
        if has_multiline_layout:
            insertion = f"{child_indent}{property_text}{newline}"
            return source[:closing_line_start] + insertion + source[closing_line_start:]
        return source[: root_start + 1] + property_text + source[root_end:]

    last_property = properties[-1]
    updated_source = source
    trailing_comma = last_property.delimiter == ","

    if not has_multiline_layout:
        insertion_point = root_end
        if not trailing_comma:
            updated_source = updated_source[: last_property.value_end] + "," + updated_source[last_property.value_end :]
            insertion_point += 1
        prefix = " " if updated_source[insertion_point - 1] not in "{[ \t\r\n" else ""
        suffix = "," if trailing_comma else ""
        return updated_source[:insertion_point] + f"{prefix}{property_text}{suffix}" + updated_source[insertion_point:]

    insertion_point = closing_line_start
    if not trailing_comma:
        updated_source = updated_source[: last_property.value_end] + "," + updated_source[last_property.value_end :]
        if insertion_point > last_property.value_end:
            insertion_point += 1

    if has_multiline_layout and closing_brace_on_own_line:
        suffix = "," if trailing_comma else ""
        insertion = f"{child_indent}{property_text}{suffix}{newline}"
        return updated_source[:insertion_point] + insertion + updated_source[insertion_point:]

    if has_multiline_layout:
        insertion_point = root_end
        if not trailing_comma:
            insertion_point += 1
        suffix = "," if trailing_comma else ""
        insertion = f"{newline}{child_indent}{property_text}{suffix}{newline}{root_indent}"
        return updated_source[:insertion_point] + insertion + updated_source[insertion_point:]


def load_first_opencode_user_config(
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> OpenCodeConfigProbeResult:
    active_logger = logger_instance or logger
    result = OpenCodeConfigProbeResult()

    for config_path in get_opencode_config_paths(home):
        if not config_path.exists():
            continue

        result.existing_paths.append(config_path)
        try:
            content = config_path.read_text(encoding="utf-8")
            result.config = parse_jsonc_object(content)
            result.content = content
            result.path = config_path
            return result
        except Exception as exc:
            active_logger.warning(f"Failed to load {config_path}: {exc}")
            result.errors.append((config_path, str(exc)))
            continue

    return result


def _load_or_create_user_config(
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> tuple[Dict[str, Any], Path]:
    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)

    if probe.path is not None and probe.config is not None:
        return probe.config, probe.path
    if probe.existing_paths:
        raise ValueError("Existing OpenCode config could not be parsed")
    return {}, get_opencode_config_paths(home)[0]


def _get_provider_config(config: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    provider_map = config.setdefault("provider", {})
    if not isinstance(provider_map, dict):
        raise ValueError("OpenCode config field 'provider' is not an object")

    provider_config = provider_map.setdefault(provider_id, {})
    if not isinstance(provider_config, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' config is not an object")
    return provider_config


def _write_opencode_config(config: Dict[str, Any], target_path: Path) -> Path:
    if "$schema" not in config:
        config["$schema"] = "https://opencode.ai/config.json"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target_path


def _prune_empty_provider_config(config: Dict[str, Any], provider_id: str) -> None:
    provider_map = config.get("provider")
    if not isinstance(provider_map, dict):
        return
    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return
    if not provider_config:
        provider_map.pop(provider_id, None)
    if not provider_map:
        config.pop("provider", None)


def _normalize_model_id(model_id: str, *, provider_id: str | None = None) -> str:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id is required")
    candidate = model_id.strip()
    normalized_provider = provider_id.strip().lower() if isinstance(provider_id, str) else ""
    if normalized_provider and candidate.lower().startswith(f"{normalized_provider}/"):
        raise ValueError("model_id must not include a provider prefix")
    return candidate


def _normalize_custom_provider_id(provider_id: str, *, reject_reserved: bool = False) -> str:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id is required")
    candidate = provider_id.strip().lower()
    if len(candidate) > 64:
        raise ValueError("provider_id is too long")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", candidate):
        raise ValueError("provider_id must use lowercase letters, numbers, dot, hyphen, or underscore")
    if reject_reserved and candidate in _RESERVED_PROVIDER_IDS:
        raise ValueError("provider_id already exists")
    return candidate


def is_reserved_opencode_provider_id(provider_id: str) -> bool:
    if not isinstance(provider_id, str):
        return False
    return provider_id.strip().lower() in _RESERVED_PROVIDER_IDS


def get_opencode_custom_provider_adapter(
    provider_id: str,
    provider_config: Dict[str, Any],
) -> Optional[str]:
    """Return the compatible-provider adapter for a user custom provider.

    ``vibe_remote.custom`` is the strongest marker, but OpenCode may normalize
    config through its own schema and drop unknown metadata. A non-reserved
    provider block using one of the compatible AI SDK packages with a baseURL is
    still a user-created compatible provider and must stay visible in Settings.
    """

    if not isinstance(provider_id, str) or not isinstance(provider_config, dict):
        return None
    if is_reserved_opencode_provider_id(provider_id):
        return None

    meta = provider_config.get(_CUSTOM_PROVIDER_META_KEY)
    if isinstance(meta, dict) and meta.get("custom") is True:
        adapter = meta.get("adapter")
        if isinstance(adapter, str) and adapter in _CUSTOM_PROVIDER_ADAPTERS:
            return adapter

    npm = provider_config.get("npm")
    adapter = next(
        (
            adapter_key
            for adapter_key, package_name in _CUSTOM_PROVIDER_ADAPTERS.items()
            if npm == package_name
        ),
        None,
    )
    if adapter is None:
        return None
    options = provider_config.get("options")
    base_url = options.get("baseURL") if isinstance(options, dict) else None
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    return adapter


def _ensure_custom_provider_meta(
    provider_id: str,
    provider_config: Dict[str, Any],
) -> None:
    adapter = get_opencode_custom_provider_adapter(provider_id, provider_config)
    if adapter is None:
        return
    meta = provider_config.get(_CUSTOM_PROVIDER_META_KEY)
    if isinstance(meta, dict) and meta.get("custom") is True:
        return
    provider_config[_CUSTOM_PROVIDER_META_KEY] = {
        "custom": True,
        "adapter": adapter,
        "adapter_label": _CUSTOM_PROVIDER_LABELS[adapter],
    }


def _normalize_custom_provider_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    candidate = name.strip()
    if len(candidate) > 80:
        raise ValueError("name is too long")
    return candidate


def _normalize_custom_provider_adapter(adapter: str) -> str:
    if not isinstance(adapter, str):
        raise ValueError("adapter is required")
    candidate = adapter.strip().lower()
    if candidate not in _CUSTOM_PROVIDER_ADAPTERS:
        raise ValueError("adapter must be openai-compatible or anthropic-compatible")
    return candidate


def _normalize_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required")
    candidate = base_url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return candidate


def _provider_uses_anthropic_thinking(provider_id: str, provider_config: Dict[str, Any]) -> bool:
    if isinstance(provider_id, str) and provider_id.strip().lower() == "anthropic":
        return True
    meta = provider_config.get(_CUSTOM_PROVIDER_META_KEY)
    if isinstance(meta, dict) and meta.get("adapter") == "anthropic-compatible":
        return True
    npm = provider_config.get("npm")
    return isinstance(npm, str) and npm == _CUSTOM_PROVIDER_ADAPTERS["anthropic-compatible"]


def _normalize_reasoning_variants(
    reasoning_efforts: Any,
    *,
    provider_id: str,
    provider_config: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    if reasoning_efforts is None:
        return {}
    if not isinstance(reasoning_efforts, list):
        raise ValueError("reasoning_efforts must be a list")
    variants: Dict[str, Dict[str, Any]] = {}
    uses_anthropic_thinking = _provider_uses_anthropic_thinking(provider_id, provider_config)
    for raw in reasoning_efforts:
        if not isinstance(raw, str):
            raise ValueError("reasoning_efforts entries must be strings")
        effort = raw.strip()
        if not effort:
            continue
        if effort not in _VALID_REASONING_VARIANTS:
            raise ValueError(f"unsupported reasoning effort: {effort}")
        if uses_anthropic_thinking:
            variants[effort] = {"thinking": {"type": "enabled", "effort": effort}}
        else:
            variants[effort] = {"reasoningEffort": effort}
    return variants


def _is_vibe_user_model(model_id: str, model_info: Dict[str, Any]) -> bool:
    meta = model_info.get(_CUSTOM_PROVIDER_META_KEY)
    if isinstance(meta, dict) and meta.get("user_model") is True:
        return True
    # Backward compatibility for user models written earlier in this PR before
    # the explicit marker existed. Normal built-in overrides usually set only
    # options/variants; Vibe-created rows carried their own id/name.
    return model_info.get("id") == model_id and model_info.get("name") == model_id


def read_opencode_provider_user_models(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return models explicitly configured under ``provider.<id>.models``.

    OpenCode merges its built-in catalog with this user config block.
    We mark these rows as user-managed in the Settings UI; built-in rows
    are read-only because OpenCode currently exposes provider-level
    disablement, not a stable "hide one built-in model" option.
    """

    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)
    if probe.config is None:
        return {}
    provider_map = probe.config.get("provider")
    if not isinstance(provider_map, dict):
        return {}
    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return {}
    models = provider_config.get("models")
    if not isinstance(models, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for model_id, model_info in models.items():
        if not isinstance(model_id, str) or not isinstance(model_info, dict):
            continue
        if _is_vibe_user_model(model_id, model_info):
            out[model_id] = model_info
    return out


def read_opencode_custom_providers(
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Dict[str, Dict[str, Any]]:
    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)
    if probe.config is None:
        return {}
    provider_map = probe.config.get("provider")
    if not isinstance(provider_map, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for provider_id, provider_config in provider_map.items():
        if not isinstance(provider_id, str) or not isinstance(provider_config, dict):
            continue
        if get_opencode_custom_provider_adapter(provider_id, provider_config) is None:
            continue
        out[provider_id] = provider_config
    return out


def is_opencode_custom_provider(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> bool:
    return provider_id in read_opencode_custom_providers(home=home, logger_instance=logger_instance)


def upsert_opencode_custom_provider(
    provider_id: str,
    name: str,
    adapter: str,
    base_url: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Path:
    active_logger = logger_instance or logger
    provider_id = _normalize_custom_provider_id(provider_id, reject_reserved=True)
    name = _normalize_custom_provider_name(name)
    adapter = _normalize_custom_provider_adapter(adapter)
    base_url = _normalize_base_url(base_url)

    config, target_path = _load_or_create_user_config(home=home, logger_instance=active_logger)
    provider_config = _get_provider_config(config, provider_id)
    existing_meta = provider_config.get(_CUSTOM_PROVIDER_META_KEY)
    if provider_config and not (isinstance(existing_meta, dict) and existing_meta.get("custom") is True):
        raise ValueError("provider_id already exists")

    options = provider_config.setdefault("options", {})
    if not isinstance(options, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' options are not an object")

    provider_config["name"] = name
    provider_config["npm"] = _CUSTOM_PROVIDER_ADAPTERS[adapter]
    options["baseURL"] = base_url
    provider_config[_CUSTOM_PROVIDER_META_KEY] = {
        "custom": True,
        "adapter": adapter,
        "adapter_label": _CUSTOM_PROVIDER_LABELS[adapter],
    }
    models = provider_config.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' models are not an object")
    return _write_opencode_config(config, target_path)


def remove_opencode_custom_provider(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Optional[Path]:
    active_logger = logger_instance or logger
    provider_id = _normalize_custom_provider_id(provider_id)
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)
    if probe.path is None or probe.config is None:
        return None

    config = probe.config
    provider_map = config.get("provider")
    if not isinstance(provider_map, dict):
        return probe.path
    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return probe.path
    if get_opencode_custom_provider_adapter(provider_id, provider_config) is None:
        raise ValueError("Only custom providers can be removed")
    provider_map.pop(provider_id, None)
    if not provider_map:
        config.pop("provider", None)
    return _write_opencode_config(config, probe.path)


def upsert_opencode_provider_model(
    provider_id: str,
    model_id: str,
    *,
    reasoning_efforts: Any = None,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Path:
    active_logger = logger_instance or logger
    model_id = _normalize_model_id(model_id, provider_id=provider_id)
    config, target_path = _load_or_create_user_config(home=home, logger_instance=active_logger)

    provider_config = _get_provider_config(config, provider_id)
    variants = _normalize_reasoning_variants(
        reasoning_efforts,
        provider_id=provider_id,
        provider_config=provider_config,
    )
    models = provider_config.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' models are not an object")

    model_config = models.setdefault(model_id, {})
    if not isinstance(model_config, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' model '{model_id}' is not an object")

    model_config.update(
        {
            "id": model_id,
            "name": model_id,
            _CUSTOM_PROVIDER_META_KEY: {"user_model": True},
        }
    )
    if variants:
        model_config["variants"] = variants
    else:
        model_config.pop("variants", None)

    return _write_opencode_config(config, target_path)


def remove_opencode_provider_model(
    provider_id: str,
    model_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Optional[Path]:
    active_logger = logger_instance or logger
    model_id = _normalize_model_id(model_id, provider_id=provider_id)
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)

    if probe.path is None or probe.config is None:
        return None

    config = probe.config
    provider_map = config.get("provider")
    if not isinstance(provider_map, dict):
        return probe.path
    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return probe.path
    models = provider_config.get("models")
    if not isinstance(models, dict):
        return probe.path
    models.pop(model_id, None)
    if not models:
        provider_config["models"] = {}

    _prune_empty_provider_config(config, provider_id)
    return _write_opencode_config(config, probe.path)


def upsert_opencode_provider_api_key(
    provider_id: str,
    api_key: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Path:
    active_logger = logger_instance or logger
    config, target_path = _load_or_create_user_config(home=home, logger_instance=active_logger)
    provider_config = _get_provider_config(config, provider_id)

    options = provider_config.setdefault("options", {})
    if not isinstance(options, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' options are not an object")

    options["apiKey"] = api_key
    _ensure_custom_provider_meta(provider_id, provider_config)
    return _write_opencode_config(config, target_path)


def remove_opencode_provider_api_key(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Optional[Path]:
    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)

    if probe.path is None or probe.config is None:
        return None

    config = probe.config
    provider_map = config.get("provider")
    if not isinstance(provider_map, dict):
        return probe.path

    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return probe.path

    options = provider_config.get("options")
    if isinstance(options, dict):
        options.pop("apiKey", None)
        if not options:
            provider_config.pop("options", None)

    _prune_empty_provider_config(config, provider_id)
    return _write_opencode_config(config, probe.path)


def upsert_opencode_provider_base_url(
    provider_id: str,
    base_url: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Path:
    """Persist a provider's custom ``baseURL`` into ``opencode.json``.

    OpenCode's own auth endpoint (``PUT /auth/{provider_id}``) only stores
    the API key; the per-provider ``baseURL`` lives in the user config
    under ``provider.<id>.options.baseURL`` (the standard Vercel AI SDK
    field name, capital URL). The Settings UI surfaces a Base URL input,
    so we mirror the api-key helper to write that field through.
    """

    active_logger = logger_instance or logger
    config, target_path = _load_or_create_user_config(home=home, logger_instance=active_logger)
    provider_config = _get_provider_config(config, provider_id)

    options = provider_config.setdefault("options", {})
    if not isinstance(options, dict):
        raise ValueError(f"OpenCode provider '{provider_id}' options are not an object")

    options["baseURL"] = base_url
    _ensure_custom_provider_meta(provider_id, provider_config)
    return _write_opencode_config(config, target_path)


def remove_opencode_provider_base_url(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """Drop a provider's custom ``baseURL`` from ``opencode.json``.

    Mirrors :func:`remove_opencode_provider_api_key`: prunes empty
    ``options`` / provider / ``provider`` blocks so the file does not
    accumulate empty scaffolding once every override is cleared.
    """

    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)

    if probe.path is None or probe.config is None:
        return None

    config = probe.config
    provider_map = config.get("provider")
    if not isinstance(provider_map, dict):
        return probe.path

    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return probe.path

    options = provider_config.get("options")
    if isinstance(options, dict):
        options.pop("baseURL", None)
        if not options:
            provider_config.pop("options", None)

    _prune_empty_provider_config(config, provider_id)
    return _write_opencode_config(config, probe.path)


def get_opencode_auth_path(home: Path | None = None) -> Path:
    """Return the absolute path to OpenCode's per-provider auth bundle.

    OpenCode stores ``{providerId: {type: "api"|"oauth", key?, ...}}`` at
    ``~/.local/share/opencode/auth.json``. The Settings UI uses this to
    render a masked preview ("``sk-proj-•••H8mN``") for each configured
    cloud provider — mirroring the Claude / Codex pages so the user can
    see at a glance which providers carry a stored key without having to
    expand each card.
    """
    resolved_home = home or Path.home()
    return resolved_home / ".local" / "share" / "opencode" / "auth.json"


def read_opencode_provider_keys(
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Dict[str, Optional[str]]:
    """Return ``{provider_id: plaintext_key | None}`` from auth.json.

    Plaintext keys never leave the server: ``vibe.api.get_opencode_providers``
    pipes each value through ``_mask_api_key`` before forwarding to the
    Settings UI. ``None`` entries mark OAuth-type providers (or any
    other ``type`` that doesn't carry a static key) — the UI can use
    presence-vs-None to decide whether to show a masked preview vs the
    "signed in via OAuth" affordance.

    A missing or unparseable file returns an empty dict; callers should
    not treat that as an error since OpenCode lazily creates the file
    on first ``PUT /auth/<id>`` call.
    """
    entries = read_opencode_provider_auth_entries(
        home=home, logger_instance=logger_instance
    )
    out: Dict[str, Optional[str]] = {}
    for provider_id, entry in entries.items():
        if entry.get("type") == "api":
            key = entry.get("key")
            out[provider_id] = key if isinstance(key, str) and key else None
        else:
            out[provider_id] = None
    return out


def read_opencode_provider_auth_entries(
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return the raw ``auth.json`` entries keyed by provider_id.

    Useful when callers need both the ``type`` (``"api"`` / ``"oauth"`` /
    other) AND the optional key — the Settings UI surfaces "currently
    active: OAuth" / "API key" badges from the ``type`` field. Plaintext
    secrets stay in process; ``vibe.api.get_opencode_providers`` masks
    or strips before serialising.
    """
    active_logger = logger_instance or logger
    path = get_opencode_auth_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        active_logger.debug("OpenCode auth.json read failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for provider_id, entry in data.items():
        if not isinstance(provider_id, str) or not isinstance(entry, dict):
            continue
        out[provider_id] = entry
    return out


def read_opencode_provider_base_url(
    provider_id: str,
    *,
    home: Path | None = None,
    logger_instance: Optional[logging.Logger] = None,
) -> Optional[str]:
    """Return the persisted ``baseURL`` for a provider, if any.

    Used by the providers-listing endpoint so the Settings UI can
    pre-populate the Base URL input with the user's last saved value
    instead of starting empty on every reload.
    """

    active_logger = logger_instance or logger
    probe = load_first_opencode_user_config(home=home, logger_instance=active_logger)
    if probe.config is None:
        return None
    provider_map = probe.config.get("provider")
    if not isinstance(provider_map, dict):
        return None
    provider_config = provider_map.get(provider_id)
    if not isinstance(provider_config, dict):
        return None
    options = provider_config.get("options")
    if not isinstance(options, dict):
        return None
    value = options.get("baseURL")
    return value if isinstance(value, str) and value.strip() else None
