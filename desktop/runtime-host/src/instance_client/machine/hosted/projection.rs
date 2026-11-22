//! Producer projections, not generic HTTP error bodies. Wire references: claims/service.ts
//! (d4c4d65), claims/name-admission.ts and hostnames/{policy,claim-projection}.ts (375b1bf).
use super::{bad, changed, text, Account, MachineTicket, Result};
use chrono::DateTime;
use serde::{Deserialize, Deserializer, Serialize};
use std::collections::HashSet;

// Missing isn't the producer's deliberate null (no prefill, no date, etc.).
fn nullable<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> std::result::Result<Option<T>, D::Error> {
    Option::deserialize(d)
}
fn supplied<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> std::result::Result<Option<T>, D::Error> {
    T::deserialize(d).map(Some)
}
fn prefix(s: &str) -> bool {
    (2..=46).contains(&s.len())
        && !s.starts_with('-')
        && !s.ends_with('-')
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}
pub(super) fn new_prefix(s: &str) -> bool {
    prefix(s) && format!("{s}-app").as_bytes().get(2..4) != Some(b"--")
}
fn host(s: &str) -> String {
    format!("{s}-app.avibe.bot")
}
fn date(s: Option<&str>) -> bool {
    s.is_none_or(|s| {
        DateTime::parse_from_rfc3339(s)
            .is_ok_and(|v| v.to_utc().to_rfc3339_opts(chrono::SecondsFormat::Millis, true) == s)
    })
}
fn masked(s: &str) -> bool {
    fn initial(s: &str) -> bool {
        s.strip_suffix("***")
            .is_some_and(|c| c.chars().count() == 1 && c.chars().all(char::is_alphanumeric))
    }
    s == "***" || s.split_once('@').is_some_and(|(a, b)| initial(a) && initial(b))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Holding {
    pub(crate) limit: u32,
    pub(crate) held_count: u32,
    pub(crate) active_count: u32,
    pub(crate) at_limit: bool,
    #[serde(deserialize_with = "nullable")]
    reason: Option<Reason>,
    #[serde(deserialize_with = "nullable")]
    pub(crate) earliest_release_at: Option<String>,
}
#[derive(Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Reason {
    AllActive,
    IncludesWaiting,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct EligibleName {
    pub(crate) prefix: String,
    pub(crate) hostname: String,
    #[serde(deserialize_with = "nullable")]
    pub(crate) release_at: Option<String>,
}
#[derive(Deserialize, PartialEq, Eq, Clone, Copy)]
#[serde(rename_all = "snake_case")]
enum Requested {
    NotSelected,
    OwnReclaimable,
    Taken,
    Public,
}
#[derive(Serialize)]
pub(crate) struct Names {
    pub(crate) holding: Holding,
    pub(crate) eligible: Vec<EligibleName>,
}
impl Names {
    fn checked(self, selected: Option<&str>, taken: bool, state: Requested) -> Result<Self> {
        let h = &self.holding;
        let reason = if !h.at_limit {
            None
        } else if h.active_count == h.held_count {
            Some(Reason::AllActive)
        } else {
            Some(Reason::IncludesWaiting)
        };
        let mut unique = HashSet::new();
        if h.limit == 0
            || h.active_count > h.held_count
            || h.at_limit != (h.held_count >= h.limit)
            || h.reason != reason
            || !date(h.earliest_release_at.as_deref())
            || (h.held_count == h.active_count && h.earliest_release_at.is_some())
            || self.eligible.len() > (h.held_count - h.active_count) as usize
            || self.eligible.iter().any(|n| {
                !new_prefix(&n.prefix)
                    || n.hostname != host(&n.prefix)
                    || !date(n.release_at.as_deref())
                    || !unique.insert(n.prefix.as_str())
            })
        {
            return Err(bad());
        }
        let expected = match selected {
            None if !taken => Requested::NotSelected,
            None => return Err(bad()),
            Some(_) if taken => Requested::Taken,
            Some(p) if unique.contains(p) => Requested::OwnReclaimable,
            Some(_) => Requested::Public,
        };
        if state != expected {
            return Err(bad());
        }
        Ok(self)
    }
}

#[derive(PartialEq, Eq)]
pub(super) struct Target {
    pub(super) prefix: String,
    pub(super) hostname: String,
    pub(super) existing_id: Option<String>,
}
// No Serialize or Debug: immutable IDs and the machine ticket aren't presentation fields.
pub(crate) enum Screen {
    New {
        prefilled: Option<String>,
        selected: Option<String>,
        taken: bool,
        names: Option<Names>,
    },
    Existing {
        prefix: String,
        hostname: String,
    },
    OtherAccount {
        masked_hint: String,
    },
    Capacity {
        prefilled: Option<String>,
        names: Option<Names>,
    },
}
pub(super) struct Read {
    pub(super) screen: Screen,
    pub(super) target: Option<Target>,
}
#[derive(Deserialize)]
struct Envelope {
    device_name: String,
    mode: String,
    locale: String,
    expires_at: String,
    account_email: String,
    account_subject: String,
    #[serde(flatten)]
    state: State,
}
#[derive(Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
enum State {
    New {
        #[serde(deserialize_with = "nullable")]
        prefilled_prefix: Option<String>,
        #[serde(deserialize_with = "nullable")]
        prefix: Option<String>,
        taken: bool,
        #[serde(default, deserialize_with = "supplied")]
        holding: Option<Holding>,
        #[serde(default, deserialize_with = "supplied")]
        reclaimable_names: Option<Vec<EligibleName>>,
        #[serde(default, deserialize_with = "supplied")]
        requested_name_state: Option<Requested>,
    },
    Existing {
        hostname: String,
        prefix: String,
        existing_instance_id: String,
    },
    OtherAccount {
        owner_hint: String,
    },
    Granted {
        hostname: String,
    },
}
pub(super) enum Evidence {
    Review(Read),
    RecordedHostname(String),
}
pub(super) fn parse(
    body: &[u8],
    ticket: &MachineTicket,
    locale: &str,
    account: &Account,
    requested: Option<&str>,
) -> Result<Evidence> {
    let e: Envelope = serde_json::from_slice(body).map_err(|_| bad())?;
    if e.mode != "desktop"
        || e.locale != locale
        || e.expires_at != ticket.expires_at
        || e.device_name != ticket.device_name
        || e.account_subject != account.id
        || e.account_email != account.email
    {
        return Err(changed());
    }
    let (screen, target) = match e.state {
        State::New {
            prefilled_prefix,
            prefix,
            taken,
            holding,
            reclaimable_names,
            requested_name_state,
        } => {
            if prefilled_prefix.as_deref().is_some_and(|s| !new_prefix(s))
                || prefix.as_deref() != requested.or(prefilled_prefix.as_deref())
                || (prefix.is_none() && taken)
            {
                return Err(bad());
            }
            let names = match (holding, reclaimable_names, requested_name_state) {
                (None, None, None) => None,
                (Some(holding), Some(eligible), Some(state)) => {
                    if holding.at_limit && matches!(state, Requested::Public | Requested::NotSelected) {
                        return Err(bad());
                    }
                    Some(Names { holding, eligible }.checked(prefix.as_deref(), taken, state)?)
                }
                _ => return Err(bad()),
            };
            let target = prefix.as_ref().filter(|_| !taken).map(|s| Target {
                prefix: s.clone(),
                hostname: host(s),
                existing_id: None,
            });
            (
                Screen::New {
                    prefilled: prefilled_prefix,
                    selected: prefix,
                    taken,
                    names,
                },
                target,
            )
        }
        State::Existing {
            hostname,
            prefix: p,
            existing_instance_id,
        } => {
            if !prefix(&p) || hostname != host(&p) || !text(&existing_instance_id, 120) {
                return Err(bad());
            }
            let target = Target {
                prefix: p.clone(),
                hostname: hostname.clone(),
                existing_id: Some(existing_instance_id),
            };
            (Screen::Existing { prefix: p, hostname }, Some(target))
        }
        State::OtherAccount { owner_hint } if masked(&owner_hint) => (
            Screen::OtherAccount {
                masked_hint: owner_hint,
            },
            None,
        ),
        State::Granted { hostname } if hostname.strip_suffix("-app.avibe.bot").is_some_and(prefix) => {
            return Ok(Evidence::RecordedHostname(hostname))
        }
        _ => return Err(bad()),
    };
    Ok(Evidence::Review(Read { screen, target }))
}

pub(super) fn grant_matches(body: &[u8], target: &Target) -> bool {
    // service.grant returns either the two decision fields, or the complete extension
    // for a new name. No redirects, callbacks, unrecognized fields or partial extensions.
    #[derive(Deserialize)]
    #[serde(untagged, deny_unknown_fields)]
    enum Receipt<'a> {
        Plain {
            status: &'a str,
            hostname: &'a str,
        },
        New {
            status: &'a str,
            hostname: &'a str,
            holding: Holding,
            reclaimable_names: Vec<EligibleName>,
            #[serde(deserialize_with = "nullable")]
            prefilled_prefix: Option<String>,
            requested_name_state: Requested,
        },
    }
    match serde_json::from_slice::<Receipt>(body) {
        Ok(Receipt::Plain {
            status: "granted",
            hostname,
        }) => hostname == target.hostname,
        Ok(Receipt::New {
            status: "granted",
            hostname,
            holding,
            reclaimable_names: eligible,
            prefilled_prefix,
            requested_name_state: state,
        }) => {
            target.existing_id.is_none()
                && hostname == target.hostname
                && prefilled_prefix.as_deref().is_none_or(new_prefix)
                && (Names { holding, eligible })
                    .checked(Some(&target.prefix), state == Requested::Taken, state)
                    .is_ok()
        }
        _ => false,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Capacity {
    error: String,
    limit: u32,
    held_count: u32,
    #[serde(deserialize_with = "nullable")]
    earliest_release_at: Option<String>,
    holding: Holding,
    reclaimable_names: Vec<EligibleName>,
    #[serde(deserialize_with = "nullable")]
    prefilled_prefix: Option<String>,
    requested_name_state: Requested,
}
pub(super) fn capacity(body: &[u8], requested: Option<&str>) -> Result<Read> {
    let c: Capacity = serde_json::from_slice(body).map_err(|_| bad())?;
    if c.error != "name_holding_limit_reached"
        || !c.holding.at_limit
        || c.limit != c.holding.limit
        || c.held_count != c.holding.held_count
        || c.earliest_release_at != c.holding.earliest_release_at
        || c.prefilled_prefix.as_deref().is_some_and(|s| !new_prefix(s))
        || !matches!(c.requested_name_state, Requested::Public | Requested::NotSelected)
    {
        return Err(bad());
    }
    let names = Names {
        holding: c.holding,
        eligible: c.reclaimable_names,
    }
    .checked(
        requested.or(c.prefilled_prefix.as_deref()),
        false,
        c.requested_name_state,
    )?;
    Ok(Read {
        screen: Screen::Capacity {
            prefilled: c.prefilled_prefix,
            names: Some(names),
        },
        target: None,
    })
}
