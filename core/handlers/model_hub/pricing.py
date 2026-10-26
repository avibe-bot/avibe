"""List-API-price valuation of metered usage, and subscription fees to compare it with.

A valuation, never a bill: it answers "what would this usage have cost at the
vendor's published API price", so a subscriber can see whether the plan pays for
itself. Nothing in routing, admission, or cooldown reads it. Hop resolution only
reports where a model's price steps up with its context (`context_price_steps`),
which Vibey's context management keeps below (C-9 `context.md` section 1).

Prices come from two places, highest precedence first: the user's hand-edited
override file (`<state dir>/model_hub_prices.json`), then the models.dev catalog
Avibe already caches for the model picker, read from the model's first-party
provider only. Every day is priced at the current table — the page says which
table that is — because the table is what we have, not what was in force then.

A call whose context input exceeds one of the model's context tiers is billed
whole at that tier's rates. A ledger row holds many calls, so the tier is
decided when a call is recorded and kept in the row; valuation reads it back.

A model with no price is never priced as zero: it is left out and its tokens are
counted, so a total can say how much it does not cover.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Optional

from vibe.backend_model_catalog import catalog_model_id

logger = logging.getLogger(__name__)

PRICE_OVERRIDE_FILENAME: Final = "model_hub_prices.json"
CURRENCY: Final = "USD"
# A price is USD per million tokens. Anything past this is a typo or a hostile
# file, not a price; the entry is ignored rather than dominating every total.
_MAX_PRICE_PER_MTOK: Final = 100_000.0
_MAX_FEE_USD: Final = 100_000.0
_MAX_KEY_CHARS: Final = 128
_MAX_INT_DIGITS: Final = 400  # below Python's lowest settable int-to-str digit limit (640)
# An alias may point at another alias, but not forever.
_MAX_ALIAS_HOPS: Final = 4
# One-hour cache writes cost twice the input price where the table says nothing.
_CACHE_WRITE_1H_INPUT_MULTIPLE: Final = 2.0
# The context size models.dev's legacy `cost.context_over_200k` names.
_LEGACY_CONTEXT_TIER_SIZE: Final = 200_000

# Model IDs the agents send that models.dev does not list under that name.
# Keys are already normalized (lowercase, no date or bracket suffix).
MODEL_ALIASES: Final[Mapping[str, str]] = {
    # Claude Code ships a model before models.dev lists it; the predecessor's
    # price is the closest published figure, and the override file can correct it.
    "claude-opus-5-5": "claude-opus-5",
    "claude-sonnet-5-5": "claude-sonnet-5",
    # Codex CLI names.
    "gpt-5-codex": "gpt-5",
    "gpt-5.1-codex": "gpt-5.1",
    "gpt-5.1-codex-max": "gpt-5.1",
    "gpt-5.1-codex-mini": "gpt-5-mini",
    "gpt-5.2-codex": "gpt-5.2",
    "codex-mini-latest": "gpt-5-mini",
    # Claude Code short names.
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

# Monthly fee per plan, USD, at the vendors' published individual monthly prices.
# Keys are what `plan_key` publishes. Sources:
# - Claude: https://claude.com/pricing (Pro $20, Max 5x $100, Max 20x $200).
# - ChatGPT: https://learn.chatgpt.com/docs/pricing (Plus $20, Pro "from $100/month"
#   with 5x or 20x Plus usage); the $100 tier is Pro 5x and $200 remains Pro 20x
#   (https://techcrunch.com/2026/04/09/chatgpt-pro-plan-100-month-codex/).
PLAN_FEES_USD: Final[Mapping[str, float]] = {
    "claude_pro": 20.0,
    "claude_max_5x": 100.0,
    "claude_max_20x": 200.0,
    "chatgpt_plus": 20.0,
    "chatgpt_pro_5x": 100.0,
    "chatgpt_pro": 200.0,
}

# (vendor family, normalized reported plan) → fee-table key.
_PLAN_KEYS: Final[Mapping[tuple[str, str], str]] = {
    ("anthropic", "pro"): "claude_pro",
    ("anthropic", "claude_pro"): "claude_pro",
    ("anthropic", "max_5x"): "claude_max_5x",
    ("anthropic", "claude_max_5x"): "claude_max_5x",
    ("anthropic", "max_20x"): "claude_max_20x",
    ("anthropic", "claude_max_20x"): "claude_max_20x",
    ("openai", "plus"): "chatgpt_plus",
    ("openai", "chatgpt_plus"): "chatgpt_plus",
    ("openai", "pro"): "chatgpt_pro",
    ("openai", "chatgpt_pro"): "chatgpt_pro",
    # Codex reports the $100 Pro 5x tier as `prolite` (openai/codex#17419).
    ("openai", "prolite"): "chatgpt_pro_5x",
    ("openai", "chatgpt_pro_5x"): "chatgpt_pro_5x",
}

# Vendors whose subscriptions have built-in plan names. Any other vendor's plan
# resolves only through a fee key the override file names outright.
_PLAN_FAMILIES: Final[Mapping[str, str]] = {"anthropic": "anthropic", "openai": "openai", "codex": "openai"}

@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    cache_write_1h: float
    cache_write_30m: float = 0.0
    cache_write_12h: float = 0.0
    # Whether the table lists a cache write above input. The one-hour figure
    # counts only when listed: its default is a multiple of input for every
    # model, so a defaulted one says nothing about the vendor.
    charges_cache_writes: bool = False
    # Higher rates past a context size, as (size, rates) by ascending size. A call
    # whose context input exceeds a size is billed whole at that tier's rates.
    context_tiers: tuple[tuple[int, "ModelPrice"], ...] = ()


def _bounded(value: object, ceiling: float) -> Optional[float]:
    """A number in [0, ceiling], or None. An int too large for a float is out of range, not an error."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0 or number > ceiling:
        return None
    return number


def _price(value: object) -> Optional[float]:
    return _bounded(value, _MAX_PRICE_PER_MTOK)


def model_price(cost: object) -> Optional[ModelPrice]:
    """Read one models.dev-shaped `cost` object, or None when it has no usable price.

    Input and output are required. A missing cache member falls back to what the
    tokens are otherwise: a cache read or write is input.

    Context tiers come from `tiers[]` entries of type `context`, each read by the
    same rules as the flat price, and from the legacy `context_over_200k` only
    when no such entry is usable. An unusable tier is ignored; tiers never price
    a model whose flat price is missing.
    """

    if not isinstance(cost, Mapping):
        return None
    price = _flat_price(cost)
    if price is None:
        return None
    tiers: dict[int, ModelPrice] = {}
    entries = cost.get("tiers")
    for entry in entries if isinstance(entries, list) else ():
        match entry:
            case {"tier": {"type": "context", "size": int() as size}} if size > 0 and not isinstance(size, bool):
                rates = _flat_price(entry)
                if rates is not None:
                    tiers.setdefault(size, rates)
    if not tiers:
        legacy = _flat_price(cost.get("context_over_200k"))
        if legacy is not None:
            tiers[_LEGACY_CONTEXT_TIER_SIZE] = legacy
    return replace(price, context_tiers=tuple(sorted(tiers.items()))) if tiers else price


def context_tier_size(price: Optional[ModelPrice], context_input_tokens: int) -> int:
    """The context tier one call falls in: the largest tier size its context input exceeds, or 0.

    Context input is the whole prompt — fresh input plus cache reads and writes —
    which is what a ledger's `input_tokens` already counts.
    """

    if price is None:
        return 0
    return max((size for size, _rates in price.context_tiers if context_input_tokens > size), default=0)


def context_price_steps(price: Optional[ModelPrice]) -> tuple[int, ...]:
    """The context sizes past which a call costs more than at the base rates, ascending.

    Each context tier with an input, cache read, or output rate above the base
    one; a tier no dearer than the base is no step. A model with no price, or a
    flat one, has none.
    """

    if price is None:
        return ()
    return tuple(
        size
        for size, rates in price.context_tiers
        if rates.input > price.input or rates.cache_read > price.cache_read or rates.output > price.output
    )


def context_tier_price(price: ModelPrice, size: int) -> ModelPrice:
    """The rates usage recorded in the context tier `size` is valued at.

    That tier's rates; or, once the table no longer lists that size, those of the
    largest listed tier below it, which the call exceeded too. Tier 0 is the base.
    """

    rates = price
    for tier_size, tier_rates in price.context_tiers:
        if tier_size <= size:
            rates = tier_rates
    return rates


def _flat_price(cost: object) -> Optional[ModelPrice]:
    if not isinstance(cost, Mapping):
        return None
    input_price = _price(cost.get("input"))
    output_price = _price(cost.get("output"))
    if input_price is None or output_price is None:
        return None
    cache_read = _price(cost.get("cache_read"))
    cache_write = _price(cost.get("cache_write"))
    cache_write_1h = _price(cost.get("cache_write_1h"))
    cache_write_30m = _price(cost.get("cache_write_30m"))
    cache_write_12h = _price(cost.get("cache_write_12h"))
    return ModelPrice(
        input=input_price,
        output=output_price,
        cache_read=input_price if cache_read is None else cache_read,
        cache_write=input_price if cache_write is None else cache_write,
        cache_write_1h=(
            input_price * _CACHE_WRITE_1H_INPUT_MULTIPLE if cache_write_1h is None else cache_write_1h
        ),
        cache_write_30m=input_price * 1.25 if cache_write_30m is None else cache_write_30m,
        cache_write_12h=input_price * 2.5 if cache_write_12h is None else cache_write_12h,
        charges_cache_writes=any(
            price is not None and price > input_price for price in (cache_write, cache_write_1h)
        ),
    )


def normalize_model_id(model_id: str) -> str:
    """Fold the spellings agents send onto the one a price table lists (``catalog_model_id``): the context-window
    tag, the snapshot date, and a provider prefix do not change a list price."""

    return catalog_model_id(model_id)


def _first_party(model_id: str, vendor_map: Mapping[str, Any]) -> Optional[str]:
    families = vendor_map.get("families")
    if not isinstance(families, list):
        return None
    matches = [
        item
        for item in families
        if isinstance(item, Mapping)
        and isinstance(item.get("prefix"), str)
        and isinstance(item.get("vendor_id"), str)
        and model_id.startswith(item["prefix"])
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item["prefix"]))["vendor_id"]


def _first_party_costs(catalog: Mapping[str, Any], vendor_map: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    """Each first-party provider's `cost` objects by model key: the part of the catalog a price is read from."""

    families = vendor_map.get("families")
    vendors = {
        item["vendor_id"]
        for item in (families if isinstance(families, list) else ())
        if isinstance(item, Mapping) and isinstance(item.get("vendor_id"), str)
    }
    costs: dict[str, dict[str, object]] = {}
    for vendor in vendors:
        provider = catalog.get(vendor)
        models = provider.get("models") if isinstance(provider, Mapping) else None
        if isinstance(models, Mapping):
            costs[vendor] = {key: entry.get("cost") for key, entry in models.items() if isinstance(entry, Mapping)}
    return costs


def plan_key(vendor: str, plan: Optional[str]) -> Optional[str]:
    """Resolve a reported plan to a fee-table key, or None when it is not a known plan."""

    if not plan:
        return None
    family = _PLAN_FAMILIES.get(vendor.strip().lower())
    if family is None:
        return None
    normalized = re.sub(r"[\s-]+", "_", plan.strip().lower())
    return _PLAN_KEYS.get((family, normalized))


def _local_day(epoch: object) -> Optional[str]:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
        return None
    try:
        return datetime.fromtimestamp(float(epoch)).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class SourcePlan:
    plan: Optional[str] = None
    renewal_day: Optional[int] = None


class PriceTable:
    """One consistent view of prices, fees, and per-Source plan settings."""

    def __init__(
        self,
        *,
        catalog: Mapping[str, Any],
        vendor_map: Mapping[str, Any],
        overrides: Mapping[str, Any],
        price_table_date: Optional[str],
        pending: bool = False,
    ):
        self.price_table_date = price_table_date
        # No models.dev table yet, and a first fetch is running that should land.
        self.pending = pending
        # Only first-party prices are ever read, so they are all a table keeps:
        # held between reads it costs kilobytes, not the parsed models.dev copy.
        # Every price is parsed here, when the table is built, which is never on the
        # controller loop (at startup, then on the usage writer's thread), so a
        # lookup on the loop is a dictionary read however large the catalog is.
        self._prices: dict[str, dict[str, Optional[ModelPrice]]] = {
            vendor: {key: model_price(cost) for key, cost in costs.items()}
            for vendor, costs in _first_party_costs(catalog, vendor_map).items()
        }
        self._vendor_map = vendor_map
        self._model_overrides: dict[str, tuple[Optional[str], Optional[ModelPrice]]] = {}
        models = overrides.get("models")
        if isinstance(models, Mapping):
            for key, entry in models.items():
                if isinstance(key, str) and len(key) <= _MAX_KEY_CHARS and isinstance(entry, Mapping):
                    alias = entry.get("alias")
                    self._model_overrides[normalize_model_id(key)] = (
                        normalize_model_id(alias) if isinstance(alias, str) and alias.strip() else None,
                        model_price(entry),
                    )
        self._fees = dict(PLAN_FEES_USD)
        plans = overrides.get("plans")
        if isinstance(plans, Mapping):
            # Keys are trimmed like a Source's `plan`; an exact key wins over a padded one.
            for key, entry in sorted(plans.items(), key=lambda item: isinstance(item[0], str) and item[0] == item[0].strip()):
                key = key.strip() if isinstance(key, str) else None
                fee = _bounded(entry.get("fee_usd"), _MAX_FEE_USD) if isinstance(entry, Mapping) else None
                if key and len(key) <= 64 and fee is not None:
                    self._fees[key] = fee
        self._sources: dict[str, SourcePlan] = {}
        sources = overrides.get("sources")
        if isinstance(sources, Mapping):
            for source_id, entry in sources.items():
                if not isinstance(source_id, str) or not isinstance(entry, Mapping):
                    continue
                plan = entry.get("plan")
                plan = plan.strip()[:64] if isinstance(plan, str) else None
                day = entry.get("renewal_day")
                self._sources[source_id] = SourcePlan(
                    plan=plan or None,
                    renewal_day=(
                        day
                        if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31
                        else None
                    ),
                )
        self._memo: dict[str, Optional[ModelPrice]] = {}

    def price(self, model_id: str) -> Optional[ModelPrice]:
        key = normalize_model_id(model_id)
        if key not in self._memo:
            self._memo[key] = self._resolve(key, hops=0)
        return self._memo[key]

    def _resolve(self, key: str, *, hops: int) -> Optional[ModelPrice]:
        if hops > _MAX_ALIAS_HOPS or not key:
            return None
        override = self._model_overrides.get(key)
        if override is not None:
            alias, price = override
            if alias is not None:
                return self._resolve(alias, hops=hops + 1)
            if price is not None:
                return price
        price = self._catalog_price(key)
        if price is not None:
            return price
        alias = MODEL_ALIASES.get(key)
        if alias is not None:
            return self._resolve(alias, hops=hops + 1)
        return None

    def _catalog_price(self, key: str) -> Optional[ModelPrice]:
        vendor = _first_party(key, self._vendor_map)
        if vendor is None:
            return None
        return self._prices.get(vendor, {}).get(key)

    def fields(self) -> dict[str, Any]:
        """The table's own part of every priced block: currency, date, and a first fetch still running."""

        fields: dict[str, Any] = {"currency": CURRENCY, "price_table_date": self.price_table_date}
        if self.pending:
            fields["pending"] = True
        return fields

    def source_plan(self, source_id: str) -> SourcePlan:
        return self._sources.get(source_id, SourcePlan())

    def fee(self, key: Optional[str]) -> Optional[float]:
        return None if key is None else self._fees.get(key)

    def resolve_plan_key(self, source_id: str, vendor: str, reported: Optional[str]) -> Optional[str]:
        """The override's plan wins over what the vendor reported."""

        configured = self.source_plan(source_id).plan
        if configured is not None:
            if configured in self._fees:
                return configured
            return plan_key(vendor, configured)
        return plan_key(vendor, reported)


@dataclass
class Cost:
    """Priced usage of one aggregate."""

    api_cost_usd: float = 0.0
    excluded_tokens: int = 0
    api_cost_lower_bound: bool = False

    def add(self, other: "Cost") -> None:
        self.api_cost_usd += other.api_cost_usd
        self.excluded_tokens += other.excluded_tokens
        self.api_cost_lower_bound = self.api_cost_lower_bound or other.api_cost_lower_bound

    def fields(self) -> dict[str, Any]:
        return {
            "api_cost_usd": round(self.api_cost_usd, 6),
            "excluded_tokens": self.excluded_tokens,
            # Anything left unpriced makes the priced figure a floor, not the value.
            "api_cost_lower_bound": self.api_cost_lower_bound or self.excluded_tokens > 0,
        }


def row_cost(row: Mapping[str, Any], price: Optional[ModelPrice]) -> Cost:
    """Price one ledger row's counters at one model's price, in the row's context tier."""

    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    # A request without a token report has usage of unknown size, never zero,
    # whether or not its model has a price.
    unreported = int(row.get("requests") or 0) > int(row.get("token_reports") or 0)
    if price is None:
        return Cost(excluded_tokens=input_tokens + output_tokens, api_cost_lower_bound=unreported)
    price = context_tier_price(price, int(row.get("context_tier") or 0))
    cache_read = min(int(row.get("cached_input_tokens") or 0), input_tokens)
    cache_write = min(int(row.get("cache_write_input_tokens") or 0), input_tokens - cache_read)
    cache_write_1h = min(int(row.get("cache_write_1h_input_tokens") or 0), cache_write)
    remaining = max(0, cache_write - cache_write_1h)
    cache_write_30m = min(int(row.get("cache_write_30m_input_tokens") or 0), remaining)
    cache_write_12h = min(int(row.get("cache_write_12h_input_tokens") or 0), remaining - cache_write_30m)
    unknown_write = max(0, remaining - cache_write_30m - cache_write_12h)
    fresh = input_tokens - cache_read - cache_write
    usd = (
        fresh * price.input
        + cache_read * price.cache_read
        + unknown_write * price.cache_write
        + cache_write_30m * price.cache_write_30m
        + cache_write_12h * price.cache_write_12h
        + cache_write_1h * price.cache_write_1h
        + output_tokens * price.output
    ) / 1_000_000
    uncaptured = int(row.get("cache_write_uncaptured_reports") or 0) > 0
    ttl_uncaptured = int(row.get("cache_write_ttl_uncaptured_reports") or 0) > 0
    return Cost(
        api_cost_usd=usd,
        api_cost_lower_bound=unreported or ttl_uncaptured or (uncaptured and price.charges_cache_writes),
    )


@dataclass
class ServiceCost:
    """Priced search-service usage of one aggregate (`web-search-services.md` §4.3).

    Two figures that are never added together: the list valuation of the billable
    requests at the current catalog price, and the vendors' own reported figures.
    """

    api_cost_usd: float = 0.0
    reported_usd: float = 0.0
    # Billable requests whose service the current catalog no longer prices.
    excluded_requests: int = 0
    api_cost_lower_bound: bool = False

    def add(self, other: "ServiceCost") -> None:
        self.api_cost_usd += other.api_cost_usd
        self.reported_usd += other.reported_usd
        self.excluded_requests += other.excluded_requests
        self.api_cost_lower_bound = self.api_cost_lower_bound or other.api_cost_lower_bound

    @property
    def lower_bound(self) -> bool:
        # Like an unpriced model's tokens, an unpriced billable request makes the figure a floor.
        return self.api_cost_lower_bound or self.excluded_requests > 0

    def fields(self) -> dict[str, Any]:
        return {
            "reported_usd": round(self.reported_usd, 6),
            "api_cost_usd": round(self.api_cost_usd, 6),
            "api_cost_lower_bound": self.lower_bound,
        }


def service_row_cost(row: Mapping[str, Any], usd_per_1k: Optional[float]) -> ServiceCost:
    """Value one service ledger row at its service's list price; None when the catalog no longer prices it.

    Every billable request is one request at the list price. A request whose
    billing is unknown adds nothing and makes the figure a floor, as a stored
    `lower_bound` does; the reported figures ride beside it unchanged.
    """

    billable = int(row.get("billable_requests") or 0)
    floor = row.get("lower_bound") is True or int(row.get("uncertain_requests") or 0) > 0
    reported = float(row.get("reported_usd") or 0.0)
    if usd_per_1k is None:
        # The catalog cannot value this service anymore. Even a non-billable
        # historical request is unpriced; retain that fact as a lower bound.
        return ServiceCost(reported_usd=reported, excluded_requests=billable, api_cost_lower_bound=True)
    return ServiceCost(api_cost_usd=billable * usd_per_1k / 1000, reported_usd=reported, api_cost_lower_bound=floor)


def billing_period(today: date, renewal_day: Optional[int]) -> tuple[str, date, Optional[date]]:
    """(basis, first day, next renewal) of the period ending today."""

    if renewal_day is None:
        return "rolling_30d", today - timedelta(days=29), None

    def on(year: int, month: int) -> date:
        # A renewal on the 31st lands on the month's last day in shorter months.
        next_first = date(year + (month == 12), month % 12 + 1, 1)
        last = (next_first - timedelta(days=1)).day
        return date(year, month, min(renewal_day, last))

    this_month = on(today.year, today.month)
    if today >= this_month:
        start = this_month
        renews = on(today.year + (today.month == 12), today.month % 12 + 1)
    else:
        previous_year, previous_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = on(previous_year, previous_month)
        renews = this_month
    return "billing_cycle", start, renews


def _parse_int(digits: str) -> int | float:
    # Any integer that cannot convert, whatever the interpreter's digit limit, is out of range.
    if len(digits) > _MAX_INT_DIGITS:
        return math.inf
    try:
        return int(digits)
    except ValueError:
        return math.inf


def _read_overrides(path: Path) -> dict[str, Any]:
    try:
        # An integer too long for the parser's digit limit becomes an out-of-range
        # number, so only its entry is rejected rather than the whole file.
        payload = json.loads(path.read_text(encoding="utf-8"), parse_int=_parse_int)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning("Model Hub price override file %s is unreadable: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        logger.warning("Model Hub price override file %s is not an object", path)
        return {}
    return payload


def load_price_table(
    state_dir: Path,
    *,
    catalog_loader: Optional[Callable[[], tuple[Mapping[str, Any], Optional[float], bool]]] = None,
    vendor_map_loader: Optional[Callable[[], Mapping[str, Any]]] = None,
) -> PriceTable:
    """Build the current table. A missing catalog leaves only the overrides to price with.

    The loader answers the catalog, when it was fetched, and whether a first copy
    is on its way; the last makes the table `pending`, so a reader can say the
    prices are being fetched rather than that there are none.
    """

    if catalog_loader is None or vendor_map_loader is None:
        from vibe import models_dev_catalog

        catalog_loader = catalog_loader or models_dev_catalog.load_models_dev_catalog_with_date
        vendor_map_loader = vendor_map_loader or models_dev_catalog.load_model_vendor_map
    try:
        catalog, fetched_at, fetching = catalog_loader()
    except Exception as exc:  # noqa: BLE001 - an optional valuation never fails a read
        logger.info("Model Hub pricing has no models.dev catalog: %s", type(exc).__name__)
        catalog, fetched_at, fetching = {}, None, False
    try:
        vendor_map = vendor_map_loader()
    except Exception:  # noqa: BLE001
        vendor_map = {}
    return PriceTable(
        catalog=catalog if isinstance(catalog, Mapping) else {},
        vendor_map=vendor_map if isinstance(vendor_map, Mapping) else {},
        overrides=_read_overrides(state_dir / PRICE_OVERRIDE_FILENAME),
        price_table_date=_local_day(fetched_at) if catalog else None,
        pending=fetching is True and not catalog,
    )


# The longest span `quota_values` sums: a billing cycle is at most 31 days.
VALUE_WINDOW_DAYS: Final = 31
_WEEK_DAYS: Final = 7


def _span(days: Mapping[str, Cost], first: date, last: date) -> Cost:
    total = Cost()
    for day, cost in days.items():
        if first.isoformat() <= day <= last.isoformat():
            total.add(cost)
    return total


def quota_values(
    sources: list[dict[str, Any]],
    *,
    daily_costs: Mapping[str, Mapping[str, Cost]],
    prices: PriceTable,
    today: date,
) -> dict[str, Any]:
    """Add each quota Source's `value` block in place and return the root totals.

    `sources` are quota-summary Source payloads; their reported `plan` is what the
    override file's plan, if any, replaces.
    """

    week_total = Cost()
    period_total = Cost()
    period_fee = 0.0
    period_sources = 0
    for source in sources:
        days = daily_costs.get(source["source_id"], {})
        basis, start, renews = billing_period(today, prices.source_plan(source["source_id"]).renewal_day)
        week = _span(days, today - timedelta(days=_WEEK_DAYS - 1), today)
        period = _span(days, start, today)
        key = prices.resolve_plan_key(source["source_id"], source["vendor"], source.get("plan"))
        fee = prices.fee(key)
        multiple = round(period.api_cost_usd / fee, 4) if fee else None
        source["value"] = {
            **prices.fields(),
            "plan_key": key,
            "fee_usd": fee,
            "multiple": multiple,
            "week": week.fields(),
            "period": {
                "basis": basis,
                "from_day": start.isoformat(),
                "to_day": today.isoformat(),
                "renews_on": renews.isoformat() if renews is not None else None,
                **period.fields(),
            },
        }
        week_total.add(week)
        if fee:
            period_total.add(period)
            period_fee += fee
            period_sources += 1
    return {
        **prices.fields(),
        "week": week_total.fields(),
        "period": (
            {
                "sources": period_sources,
                "fee_usd": round(period_fee, 6),
                "multiple": round(period_total.api_cost_usd / period_fee, 4),
                **period_total.fields(),
            }
            if period_sources
            else None
        ),
    }


# --- hosted tools (model-hub-capability-model.md §6.4) ----------------------
#
# One owner turns a hosted response's usage into money: this module. Callers
# (Vibey's hosted_summary, the gateway's ProxyExecution) only pass what the
# response reported, read and bounded where it enters
# (``core.agent_core.ai.hosted``, ``stream_wire``), so no guard is needed here.

# The provider counter that counts one unit of a hosted tool's list price.
_HOSTED_UNIT_COUNTERS: Final[Mapping[tuple[str, str], str]] = {
    ("web_search", "call"): "web_search_calls",
    ("web_search", "search"): "web_search_requests",
    ("x_search", "call"): "x_search_calls",
    ("x_search", "post"): "x_posts_fetched",
    ("x_search", "profile"): "x_users_fetched",
}


def hosted_response_cost(
    *,
    billing: str,
    reported_usd: Optional[float],
    model_price: Optional[ModelPrice],
    usage: Any,
    tool_prices: Mapping[str, Any],
    counts: Mapping[str, int],
) -> Optional[tuple[str, Optional[float]]]:
    """``(basis, usd)`` for one hosted response, or None when no price is known (never zero).

    A subscription's call is ``included``. A provider's own figure for the whole
    response is ``reported``. Otherwise the ``list`` price is the response's
    tokens at the model's rates (in its context tier) plus each used tool's list
    fee from the provider's counters; a missing token price, tool price or
    counter makes the whole response unknown rather than partly priced.
    ``usage`` is a ``ProtocolUsageReport``: its input already counts cache tokens.
    """

    if billing == "monthly":
        return ("included", None)
    if reported_usd is not None:
        return ("reported", reported_usd)
    if model_price is None or usage is None:
        return None
    row = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "cache_write_1h_input_tokens": usage.cache_write_1h_input_tokens,
        "cache_write_30m_input_tokens": usage.cache_write_30m_input_tokens,
        "cache_write_12h_input_tokens": usage.cache_write_12h_input_tokens,
        "requests": 1,
        "token_reports": 1,
        "context_tier": context_tier_size(model_price, usage.input_tokens),
    }
    usd = row_cost(row, model_price).api_cost_usd or 0.0
    for tool, price in tool_prices.items():
        if price is None:
            return None
        for unit, usd_per_1k in ((price.unit, price.usd_per_1k), *((part.unit, part.usd_per_1k) for part in price.extra)):
            counter = _HOSTED_UNIT_COUNTERS.get((tool, unit))
            if counter is None or counter not in counts:
                return None
            usd += counts[counter] * usd_per_1k / 1000
    return ("list", usd)
