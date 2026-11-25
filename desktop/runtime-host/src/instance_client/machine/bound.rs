//! Read-only observation of the binding currently present, not a receipt for a particular claim.
use super::*;
use crate::instance_client::{
    protocol::{Account, Relationship},
    transport::{FactorCookies, Response},
};
use std::collections::HashSet;

/// No identifier, URL, credential or capability escapes this observation.
#[derive(PartialEq, Eq)]
pub(crate) enum CurrentBinding {
    NoPublishedBinding,
    OwnerObserved,
    BoundOwnerNotProven,
    LegacyPending(LegacyPending),
}

/// A point-in-time local observation. No variant carries an operation, grant or write permit.
#[derive(PartialEq, Eq)]
pub(crate) enum LegacyPending {
    Wait,
    NoRequest,
    Ended,
    Expired,
    Publishing,
    Incompatible,
}

pub(super) fn catalog(response: Response, account: &Account, instance: &str) -> Result<CurrentBinding> {
    #[derive(Deserialize)]
    struct Catalog {
        account: Account,
        instances: Vec<Row>,
    }
    #[derive(Deserialize)]
    struct Row {
        id: String,
        relationship: Relationship,
    }
    if response.status != 200 {
        return Err(ClientError::new("account_observation_unavailable"));
    }
    let value: Catalog = serde_json::from_slice(&response.body).map_err(|_| ClientError::new("invalid_response"))?;
    if &value.account != account {
        return Err(retired());
    }
    let mut seen = HashSet::new();
    let mut owner = false;
    for row in value.instances {
        if !text(&row.id, 120) || !seen.insert(row.id.clone()) {
            return Err(ClientError::new("invalid_response"));
        }
        if row.id == instance && row.relationship == Relationship::Owner {
            owner = true;
        }
    }
    Ok(if owner {
        CurrentBinding::OwnerObserved
    } else {
        CurrentBinding::BoundOwnerNotProven
    })
}

pub(super) fn frame_error(frame: Frame) -> ClientError {
    match frame {
        Frame::Result {
            v: 1,
            outcome,
            code: Some(code),
        } if outcome == "error"
            && matches!(
                code.as_str(),
                "local_binding_changed" | "local_binding_unavailable" | "pairing_recovery_required" | "invalid_backend"
            ) =>
        {
            ClientError::new(code)
        }
        _ => invalid(),
    }
}

async fn inspect<R: AsyncBufRead + Unpin, W: AsyncWrite + Unpin>(
    output: &mut R,
    input: &mut W,
    account: &Account,
    jar: &mut NativeSession,
    frozen: &FactorCookies,
    check: &impl Fn() -> Result<()>,
) -> Result<CurrentBinding> {
    let first = read(output).await?;
    check()?;
    let (instance, generation) = match first {
        Frame::NoBinding { v: 1 } => return Ok(CurrentBinding::NoPublishedBinding),
        Frame::Binding {
            v: 1,
            instance_id,
            generation,
        } if text(&instance_id, 120) && nonce(&generation) => (instance_id, generation),
        // Return through the same owner-checked, single-delivery BindingObservation.
        // These codes are not valid echo/publication receipts, nor NoBinding results.
        Frame::Result {
            v: 1,
            outcome,
            code: Some(code),
        } if outcome == "error" => {
            let seen = match code.as_str() {
                "legacy_pairing_wait" => LegacyPending::Wait,
                "legacy_pairing_no_request" => LegacyPending::NoRequest,
                "legacy_pairing_ended" => LegacyPending::Ended,
                "legacy_pairing_expired" => LegacyPending::Expired,
                "legacy_pairing_publication" => LegacyPending::Publishing,
                "legacy_pairing_incompatible" => LegacyPending::Incompatible,
                _ => {
                    return Err(frame_error(Frame::Result {
                        v: 1,
                        outcome,
                        code: Some(code),
                    }))
                }
            };
            return Ok(CurrentBinding::LegacyPending(seen));
        }
        other => return Err(frame_error(other)),
    };
    // A disposable ACCOUNT jar. Never publish Set-Cookie, account/catalog state,
    // or a credential write from this GET. No fallback to a cached catalog.
    let reply = jar
        .read_frozen_account(frozen)
        .await
        .map_err(|_| ClientError::new("account_observation_unavailable"))?;
    check()?;
    let classified = catalog(reply, account, &instance)?;
    // Generated AFTER classification. A prebuffered helper frame is not a
    // recheck, even if it contains the correct generation from the first frame.
    let challenge = uuid::Uuid::new_v4().simple().to_string();
    echo(output, input, &generation, &challenge).await?;
    check()?;
    Ok(classified)
}

pub(super) async fn echo<R: AsyncBufRead + Unpin, W: AsyncWrite + Unpin>(
    output: &mut R,
    input: &mut W,
    generation: &str,
    challenge: &str,
) -> Result<()> {
    write(
        input,
        json!({"v":1,"op":"recheck","generation":generation,"challenge":challenge}),
    )
    .await?;
    match read(output).await? {
        Frame::Unchanged {
            v: 1,
            generation: g,
            challenge: c,
        } if g == generation && c == challenge => Ok(()),
        other => Err(frame_error(other)),
    }
}

pub(in crate::instance_client) async fn observe(
    source: ClaimSource,
    local_transport: Arc<dyn Transport>,
    account: &Account,
    mut jar: NativeSession,
    frozen: &FactorCookies,
    check: impl Fn() -> Result<()>,
) -> Result<CurrentBinding> {
    let ClaimSource {
        command,
        origin,
        mut live,
    } = source;
    check()?;
    if !*live.borrow() || !matches!(live.has_changed(), Ok(false)) {
        return Err(retired());
    }
    let mut child = Command::from(command.0)
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| ClientError::new("local_runtime_upgrade_required"))?;
    let mut input = child.stdin.take().ok_or_else(invalid)?;
    let mut output = BufReader::new(child.stdout.take().ok_or_else(invalid)?);
    let work = async {
        admit(
            &mut output,
            &mut input,
            &origin,
            local_transport,
            json!({"v":1,"op":"inspect","origin":origin.as_str()}),
        )
        .await?;
        timeout(
            Duration::from_secs(45),
            inspect(&mut output, &mut input, account, &mut jar, frozen, &check),
        )
        .await
        .map_err(|_| expired())?
    };
    let outcome = tokio::select! {biased;
        _ = live.changed() => Err(retired()),
        result = work => result,
    };
    drop(input);
    drop(output);
    // This child cannot claim or accept. EOF releases its read pin; on cancellation
    // kill_on_drop also bounds a child that failed to finish the read-only protocol.
    let _ = timeout(Duration::from_secs(1), child.wait()).await;
    check()?;
    if !*live.borrow() || !matches!(live.has_changed(), Ok(false)) {
        return Err(retired());
    }
    outcome
}

#[cfg(test)]
mod tests;
