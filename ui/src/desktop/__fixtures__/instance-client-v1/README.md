# Native protocol fixture provenance

The JSON files mirror `desktop/contracts/instance-client-v1/` byte for byte,
including the accepted rc19 A4 local identity, password and direct logout
receipt additions and A5 nullable local slug/full-only current-instance
metadata. All values are synthetic. Native owns these shapes;
changes are coordinated with the product consumer.
Transport fixtures remain native/backend-owned and are not browser input.

The tests consume these objects through the actual account hook and components.
They prove the UI can consume the frozen samples, not that native or cloud works.
