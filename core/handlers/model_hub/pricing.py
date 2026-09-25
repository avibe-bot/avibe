"""List-API-price valuation of metered usage, and subscription fees to compare it with.

A valuation, never a bill: it answers "what would this usage have cost at the
vendor's published API price", so a subscriber can see whether the plan pays for
itself. Nothing in resolution, admission, or cooldown reads it.

Prices come from two places, highest precedence first: the user's hand-edited
override file (`<state dir>/model_hub_prices.json`), then the models.dev catalog
Avibe already caches for the model picker, read from the model's first-party
provider only. Every day is priced at the current table — the page says which
table that is — because the table is what we have, not what was in force then.

A model with no price is never priced as zero: it is left out and its tokens are
counted, so a total can say how much it does not cover.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Optional

logger = logging.getLogger(__name__)

PRICE_OVERRIDE_FILENAME: Final = "model_hub_prices.json"
CURRENCY: Final = "USD"
# A price is USD per million tokens. Anything past this is a typo or a hostile
# file, not a price; the entry is ignored rather than dominating every total.
_MAX_PRICE_PER_MTOK: Final = 100_000.0
_MAX_FEE_USD: Final = 100_000.0
_MAX_KEY_CHARS: Final = 128
# An alias may point at another alias, but not forever.
_MAX_ALIAS_HOPS: Final = 4
# One-hour cache writes cost twice the input price where the table says nothing.
_CACHE_WRITE_1H_INPUT_MULTIPLE: Final = 2.0

# Model IDs the agents send that models.dev does not list under that name.
# Keys are already normalized (lowercase, no date or bracket suffix).
MODEL_ALIASES: Final[Mapping[str, str]] = {
    # Claude Code ships a model before models.dev lists it; the predecessor's
    # price is the closest published figure, and the override file can correct it.
    "claude-opus-5-5": "claude-opus-5",
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

# Monthly fee per plan, USD. Keys are what `plan_key` publishes.
PLAN_FEES_USD: Final[Mapping[str, float]] = {
    "claude_pro": 20.0,
    "claude_max_5x": 100.0,
    "claude_max_20x": 200.0,
    "chatgpt_plus": 20.0,
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
}

# Vendors whose subscriptions have built-in plan names. Any other vendor's plan
# resolves only through a fee key the override file names outright.
_PLAN_FAMILIES: Final[Mapping[str, str]] = {"anthropic": "anthropic", "openai": "openai", "codex": "openai"}

_DATE_SUFFIX: Final = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2}|@[^/]*)$")
_BRACKET_SUFFIX: Final = re.compile(r"\[[^\]]*\]$")


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    cache_write_1h: float
    # Whether the table lists a cache write above input. The one-hour figure
    # counts only when listed: its default is a multiple of input for every
    # model, so a defaulted one says nothing about the vendor.
    charges_cache_writes: bool = False


def _price(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > _MAX_PRICE_PER_MTOK:
        return None
    return number


def model_price(cost: object) -> Optional[ModelPrice]:
    """Read one models.dev-shaped `cost` object, or None when it has no usable price.

    Input and output are required. A missing cache member falls back to what the
    tokens are otherwise: a cache read or write is input.
    """

    if not isinstance(cost, Mapping):
        return None
    input_price = _price(cost.get("input"))
    output_price = _price(cost.get("output"))
    if input_price is None or output_price is None:
        return None
    cache_read = _price(cost.get("cache_read"))
    cache_write = _price(cost.get("cache_write"))
    cache_write_1h = _price(cost.get("cache_write_1h"))
    return ModelPrice(
        input=input_price,
        output=output_price,
        cache_read=input_price if cache_read is None else cache_read,
        cache_write=input_price if cache_write is None else cache_write,
        cache_write_1h=(
            input_price * _CACHE_WRITE_1H_INPUT_MULTIPLE if cache_write_1h is None else cache_write_1h
        ),
        charges_cache_writes=any(price is not None and price > input_price for price in (cache_write, cache_write_1h)),
    )


def normalize_model_id(model_id: str) -> str:
    """Fold the spellings agents send onto the one a price table lists.

    `anthropic/claude-opus-5[1m]` and `claude-opus-4-5-20251101` become
    `claude-opus-5` and `claude-opus-4-5`: the context-window tag, the snapshot
    date, and a provider prefix do not change a list price.
    """

    text = model_id.strip().lower()
    text = text.rsplit("/", 1)[-1]
    text = _BRACKET_SUFFIX.sub("", text)
    text = _DATE_SUFFIX.sub("", text)
    return text.strip()


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
    ):
        self.price_table_date = price_table_date
        self._catalog = catalog
        self._vendor_map = vendor_map
        self._model_overrides: dict[str, Mapping[str, Any]] = {}
        models = overrides.get("models")
        if isinstance(models, Mapping):
            for key, entry in models.items():
                if isinstance(key, str) and len(key) <= _MAX_KEY_CHARS and isinstance(entry, Mapping):
                    self._model_overrides[normalize_model_id(key)] = entry
        self._fees = dict(PLAN_FEES_USD)
        plans = overrides.get("plans")
        if isinstance(plans, Mapping):
            for key, entry in plans.items():
                fee = entry.get("fee_usd") if isinstance(entry, Mapping) else None
                if (
                    isinstance(key, str)
                    and 0 < len(key) <= 64
                    and not isinstance(fee, bool)
                    and isinstance(fee, (int, float))
                    and math.isfinite(fee)
                    and 0 <= fee <= _MAX_FEE_USD
                ):
                    self._fees[key] = float(fee)
        self._sources: dict[str, SourcePlan] = {}
        sources = overrides.get("sources")
        if isinstance(sources, Mapping):
            for source_id, entry in sources.items():
                if not isinstance(source_id, str) or not isinstance(entry, Mapping):
                    continue
                plan = entry.get("plan")
                day = entry.get("renewal_day")
                self._sources[source_id] = SourcePlan(
                    plan=plan[:64] if isinstance(plan, str) and plan.strip() else None,
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
            alias = override.get("alias")
            if isinstance(alias, str) and alias.strip():
                return self._resolve(normalize_model_id(alias), hops=hops + 1)
            price = model_price(override)
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
        provider = self._catalog.get(vendor)
        models = provider.get("models") if isinstance(provider, Mapping) else None
        if not isinstance(models, Mapping):
            return None
        entry = models.get(key)
        if not isinstance(entry, Mapping):
            return None
        return model_price(entry.get("cost"))

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
    """Price one ledger row's counters at one model's price."""

    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    # A request without a token report has usage of unknown size, never zero,
    # whether or not its model has a price.
    unreported = int(row.get("requests") or 0) > int(row.get("token_reports") or 0)
    if price is None:
        return Cost(excluded_tokens=input_tokens + output_tokens, api_cost_lower_bound=unreported)
    cache_read = min(int(row.get("cached_input_tokens") or 0), input_tokens)
    cache_write = min(int(row.get("cache_write_input_tokens") or 0), input_tokens - cache_read)
    cache_write_1h = min(int(row.get("cache_write_1h_input_tokens") or 0), cache_write)
    fresh = input_tokens - cache_read - cache_write
    usd = (
        fresh * price.input
        + cache_read * price.cache_read
        + (cache_write - cache_write_1h) * price.cache_write
        + cache_write_1h * price.cache_write_1h
        + output_tokens * price.output
    ) / 1_000_000
    uncaptured = int(row.get("cache_write_uncaptured_reports") or 0) > 0
    return Cost(
        api_cost_usd=usd,
        api_cost_lower_bound=unreported or (uncaptured and price.charges_cache_writes),
    )


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


def _read_overrides(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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
    catalog_loader: Optional[Callable[[], tuple[Mapping[str, Any], Optional[float]]]] = None,
    vendor_map_loader: Optional[Callable[[], Mapping[str, Any]]] = None,
) -> PriceTable:
    """Build the current table. A missing catalog leaves only the overrides to price with."""

    if catalog_loader is None or vendor_map_loader is None:
        from vibe import models_dev_catalog

        catalog_loader = catalog_loader or models_dev_catalog.load_models_dev_catalog_with_date
        vendor_map_loader = vendor_map_loader or models_dev_catalog.load_model_vendor_map
    try:
        catalog, fetched_at = catalog_loader()
    except Exception as exc:  # noqa: BLE001 - an optional valuation never fails a read
        logger.info("Model Hub pricing has no models.dev catalog: %s", type(exc).__name__)
        catalog, fetched_at = {}, None
    try:
        vendor_map = vendor_map_loader()
    except Exception:  # noqa: BLE001
        vendor_map = {}
    return PriceTable(
        catalog=catalog if isinstance(catalog, Mapping) else {},
        vendor_map=vendor_map if isinstance(vendor_map, Mapping) else {},
        overrides=_read_overrides(state_dir / PRICE_OVERRIDE_FILENAME),
        price_table_date=_local_day(fetched_at) if catalog else None,
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
            "currency": CURRENCY,
            "price_table_date": prices.price_table_date,
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
        "currency": CURRENCY,
        "price_table_date": prices.price_table_date,
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
