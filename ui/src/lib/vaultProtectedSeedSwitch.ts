// Whether the Add key dialog offers the Protected tier for a NEW seed-phrase wallet (Product, 2026-11-16:
// held for the 3.2.7 stable until the owner has verified it on the new secure-window build).
//
// Off: a seed-phrase wallet is offered on Standard only; every other key keeps both tiers. Existing
// protected seed wallets are untouched: they still list, sign and work as before.
// To turn the entry back on, set this to `true`.
export const PROTECTED_SEED_WALLET_ENABLED = false;
