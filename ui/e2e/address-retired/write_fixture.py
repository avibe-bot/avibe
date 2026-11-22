"""Write the exact responses a retired address gives, for the browser suite beside this file.

    python ui/e2e/address-retired/write_fixture.py

``tests/test_instance_address.py`` fails when the checked-in JSON no longer
matches what the instance produces, so the browsers always meet the real bytes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).with_name("cleaning-response.json")
RECEIPT = "R" * 43


def build() -> dict:
    sys.path.insert(0, str(REPOSITORY))
    from vibe import instance_address_responses as responses

    def fixed(status: int, content_type: str | None, body: bytes, clear: str = responses.CLEAR_SITE_DATA) -> dict:
        headers = responses.cleanup_headers("/", "", content_type=content_type, clear=clear)
        return {
            "status": status,
            "headers": [[key.decode("ascii"), value.decode("ascii")] for key, value in headers],
            "body": body.decode("utf-8"),
        }

    return {
        "receipt": RECEIPT,
        "document": fixed(
            410,
            "text/html; charset=utf-8",
            responses.cleaning_document(language="en", control_plane_origin="https://avibe.bot", receipt=RECEIPT),
        ),
        "worker": fixed(200, "text/javascript; charset=utf-8", responses.REPLACEMENT_WORKER.encode("utf-8")),
        "report": fixed(204, None, b"", responses.CLEAR_HTTP_CACHE),
    }


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
