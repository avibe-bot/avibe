"""Publication guard for the single cross-client contact asset; never sends mail."""
from vibe.support_contact import SUPPORT_EMAIL


def test_owner_configured_support_mailbox_is_release_ready():
    # Pin both parts independently; do not provide another usable address/default
    # that consumers might accidentally copy instead of reading the shared asset.
    assert SUPPORT_EMAIL.partition('@') == ('support', '@', 'avibe.bot')
