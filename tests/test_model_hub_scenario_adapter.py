"""Write-ahead credential contract for the shared Model Hub scenario fake."""

from __future__ import annotations

import asyncio
from functools import partial

import pytest

from tests.scenario_harness.model_hub import ModelHubScenarioAdapter


@pytest.fixture(params=["permanent", "transient"])
def provisioning(request):
    adapter = ModelHubScenarioAdapter()
    kwargs = {
        "vendor": "custom",
        "secret": "scenario-only-secret",
        "base_url": "https://upstream.example/v1",
    }
    if request.param == "permanent":
        kwargs["protocol"] = "openai_chat"
        provision = partial(adapter.provision_credential, **kwargs)
        ledger = adapter.provisioned
    else:
        provision = partial(adapter.provision_transient_credential, **kwargs)
        ledger = adapter.provisioned_transient
    return adapter, provision, ledger


async def test_reservation_callback_precedes_material_and_receives_returned_ref(provisioning):
    adapter, provision, ledger = provisioning
    existing_ref = await provision()
    before = (list(adapter.provisioned), list(adapter.provisioned_transient))
    reservations = []

    def on_reserved(credential_ref: str) -> None:
        assert (adapter.provisioned, adapter.provisioned_transient) == before
        assert credential_ref != existing_ref
        reservations.append(credential_ref)

    returned_ref = await provision(on_reserved=on_reserved)

    assert reservations == [returned_ref]
    assert ledger == [existing_ref, returned_ref]


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_reservation_callback_failure_preserves_provisioned_material(provisioning, error_type):
    adapter, provision, _ledger = provisioning
    await provision()
    before = (list(adapter.provisioned), list(adapter.provisioned_transient))
    reservations = []
    failure = error_type("reservation was not made durable")

    def on_reserved(credential_ref: str) -> None:
        assert (adapter.provisioned, adapter.provisioned_transient) == before
        reservations.append(credential_ref)
        raise failure

    with pytest.raises(error_type) as caught:
        await provision(on_reserved=on_reserved)

    assert caught.value is failure
    assert len(reservations) == 1
    assert (adapter.provisioned, adapter.provisioned_transient) == before


@pytest.mark.parametrize("options", [{}, {"on_reserved": None}], ids=["omitted", "explicit-none"])
async def test_provisioning_without_reservation_callback_remains_supported(provisioning, options):
    _adapter, provision, ledger = provisioning

    first = await provision(**options)
    second = await provision(**options)

    assert first != second
    assert ledger == [first, second]
