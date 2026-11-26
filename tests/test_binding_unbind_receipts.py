"""Only a matching receipt is permission to discard the original identity.

The local reset tests start after consent to local cleanup; they cannot detect
accepting another operation's receipt or a generic proxy error as that consent.
"""
import pytest


OP = '675901f5-bae4-4bad-83c6-bfa6c3620144'
OTHER = '41d8da35-78cd-43e9-b016-f29971164963'


@pytest.mark.parametrize('status,body,accepted,expected', [
    (200, f'{{"status":"unbound","operation_id":"{OP}"}}', False, 'unbound'),
    (200, f'{{"status":"unbound","operation_id":"{OTHER}"}}', False, 'repair'),
    (200, f'{{"status":"unbound","operation_id":"{OP}","release_at":"2026-12-25T00:00:00Z"}}', False, 'unbound'),
    (200, f'{{"status":"unbound","operation_id":"{OP}","release_at":null}}', False, 'unbound'),
    (200, f'{{"status":"unbound","operation_id":"{OP}","release_at":"soon"}}', False, 'repair'),
    (200, f'{{"status":"unbound","operation_id":"{OP}","hostname":"x.avibe.bot"}}', False, 'repair'),
    (200, f'{{"status":"pending","status":"unbound","operation_id":"{OP}"}}', False, 'repair'),
    (202, f'{{"status":"pending","operation_id":"{OP}"}}', True, 'pending'),
    (401, '{"error":"unbind_not_applied"}', True, 'repair'),
    (401, '{"error":"unbind_not_applied"}', False, 'invalid_credential'),
    (401, '{"error":"invalid_device_secret"}', False, 'repair'),
    (403, f'{{"error":"unbind_ineligible","operation_id":"{OP}"}}', False, 'ineligible'),
    (403, f'{{"error":"unbind_ineligible","operation_id":"{OP}"}}', True, 'repair'),
    (400, '{"error":"invalid_unbind_request"}', False, 'repair'),
    (503, '{"error":"unbind_unavailable"}', True, 'retry'),
    (503, '{"error":"unbind_unavailable","extra":true}', True, 'retry'),
    (500, '{"error":"FUNCTION_INVOCATION_TIMEOUT"}', False, 'retry'),
    (503, f'{{"status":"unbound","operation_id":"{OP}"}}', True, 'retry'),
    (503, '<html>service unavailable</html>', False, 'retry'),
    (502, '<html>bad gateway</html>', False, 'retry'),
    (522, '', True, 'retry'),
    (301, '', False, 'retry'),
    (200, '<html>login</html>', False, 'retry'),
    (404, '{"error":"not_found"}', False, 'repair'),
    (404, '<html>not found</html>', False, 'retry'),
    (500, '', False, 'retry'),
    (409, '{"error":"unbind_not_applied"}', False, 'repair'),
    (418, f'{{"status":"pending","operation_id":"{OP}"}}', False, 'repair'),
    (403, f'{{"error":"unbind_ineligible","operation_id":"{OTHER}"}}', False, 'repair'),
    (429, '', False, 'retry'),
])
def test_classify_original_response(status, body, accepted, expected):
    from vibe.binding_unbind import classify_receipt

    assert classify_receipt(status, body.encode(), OP, accepted=accepted) == expected
