//! Private, one-child machine channel. None of these types are bridge commands or runtime logs.
use super::{
    enrollment::local_mutation,
    instance_update::utc_now,
    protocol::ClientError,
    transport::{Epoch, NativeSession, Transport},
};
use crate::{launcher::ClaimHelperCommand, LoopbackOrigin, RuntimeHost};
use chrono::DateTime;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    process::Command,
    sync::{oneshot, watch},
    time::{timeout, timeout_at, Instant},
};

pub(crate) mod bound;
pub(crate) mod hosted;
pub(crate) mod publication;
use hosted::ReviewedGrant;

#[cfg(test)]
pub(crate) mod tests;

const MAX_FRAME: usize = 4096;
const ADMIT: &str = "/api/desktop/claim/v1/admit";
fn invalid() -> ClientError {
    ClientError::new("invalid_machine_response")
}
fn retired() -> ClientError {
    ClientError::new("stale_generation")
}
fn expired() -> ClientError {
    ClientError::new("expired")
}
/// This computer's Runtime has no claim protocol: it predates it, and only an
/// upgrade of that Runtime helps (`Coordinator::upgrade_local_runtime`).
pub const TOO_OLD: &str = "local_runtime_too_old";
fn too_old() -> ClientError {
    ClientError::new(TOO_OLD)
}

/// Whether the Runtime at `origin` lacks the claim protocol: its admission
/// route does not exist. Asked only after a claim pipe failed before its first
/// frame (an older CLI does not know the pipe) or the route answered 404; any
/// other answer, or none, is not taken as age.
///
/// The route only takes POST, and a GET of any API path reaches the static
/// catch-all, which answers 404 whether or not the route exists. So this asks
/// with the same owner POST (CSRF and Origin) as the Workbench: where the route
/// exists its handler refuses an empty body (400/403) without acting; where it
/// does not, the Runtime's unknown-API handler answers 404.
pub(crate) async fn claim_protocol_missing(origin: &LoopbackOrigin, transport: Arc<dyn Transport>) -> bool {
    let mut local = NativeSession::new(origin.navigation_url(), transport);
    matches!(local_mutation(&mut local, ADMIT, json!({})).await, Ok(reply) if reply.status == 404)
}
fn text(s: &str, max: usize) -> bool {
    !s.is_empty() && s.trim() == s && !s.chars().any(|c| c.is_control() || matches!(c, '\u{061c}' | '\u{200b}'..='\u{200f}' | '\u{2028}'..='\u{202e}' | '\u{2060}'..='\u{206f}' | '\u{feff}')) && s.chars().count() <= max
}

pub(crate) struct ClaimSource {
    pub(crate) command: ClaimHelperCommand,
    pub(crate) origin: LoopbackOrigin,
    pub(crate) live: watch::Receiver<bool>,
}

// No Debug, Serialize or Deserialize on the live ticket/decision. The hosted
// reader must compare its device and ticket to these private machine values.
struct MachineTicket {
    claim_id: String,
    user_code: String,
    device_name: String,
    expires_at: String,
    existing_readback: bool,
    deadline: Instant,
}

/// Local publication alone does not prove account ownership or service takeover.
pub(crate) enum MachineOutcome {
    Publication(publication::Publication),
    Declined,
}
enum Notice {
    Finished,
    Declined,
}

type Result<T> = std::result::Result<T, ClientError>;

#[derive(Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
enum Frame {
    Challenge {
        v: u8,
        challenge: String,
    },
    NoBinding {
        v: u8,
    },
    Binding {
        v: u8,
        instance_id: String,
        generation: String,
    },
    Publication {
        v: u8,
        claim_id: String,
        instance_id: String,
        generation: String,
    },
    Unchanged {
        v: u8,
        generation: String,
        challenge: String,
    },
    Ticket {
        v: u8,
        claim_id: String,
        user_code: String,
        user_url: String,
        mode: String,
        device_name: String,
        expires_at: String,
        ordinary_decision_readback: bool,
    },
    Result {
        v: u8,
        outcome: String,
        code: Option<String>,
    },
}

impl Frame {
    fn ticket(self) -> Result<MachineTicket> {
        match self {
            Self::Ticket {
                v: 1,
                claim_id,
                user_code,
                user_url,
                mode,
                device_name,
                expires_at,
                ordinary_decision_readback,
            } if mode == "desktop"
                && text(&device_name, 120)
                && uuid::Uuid::parse_str(&claim_id).is_ok_and(|id| id.to_string() == claim_id)
                && user_code.len() == 10
                && user_code
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || (b'2'..=b'9').contains(&b))
                && user_url == format!("https://avibe.bot/connect/{user_code}") =>
            {
                let end = DateTime::parse_from_rfc3339(&expires_at)
                    .map_err(|_| invalid())?
                    .to_utc();
                let remaining = (end - utc_now()).to_std().map_err(|_| expired())?;
                if remaining.is_zero() {
                    return Err(expired());
                }
                Ok(MachineTicket {
                    claim_id,
                    user_code,
                    device_name,
                    expires_at,
                    existing_readback: ordinary_decision_readback,
                    deadline: Instant::now() + remaining.min(Duration::from_secs(1800)),
                })
            }
            Self::Result {
                v: 1,
                outcome,
                code: Some(code),
            } if outcome == "error" => Err(known_error(&code)),
            _ => Err(invalid()),
        }
    }
    fn outcome(self) -> Result<Notice> {
        match self {
            Self::Result {
                v: 1,
                outcome,
                code: None,
            } if outcome == "publication_finished" => Ok(Notice::Finished),
            Self::Result {
                v: 1,
                outcome,
                code: None,
            } if outcome == "declined" => Ok(Notice::Declined),
            Self::Result {
                v: 1,
                outcome,
                code: Some(code),
            } if outcome == "error" => Err(known_error(&code)),
            _ => Err(invalid()),
        }
    }
}

fn known_error(code: &str) -> ClientError {
    match code {
        "already_bound"
        | "binding_unavailable"
        | "claim_in_progress"
        | "claim_unavailable"
        | "control_plane_unreachable"
        | "decision_changed"
        | "expired"
        | "fresh_claim_required"
        | "invalid_backend"
        | "invalid_claim_response"
        | "invalid_device_name"
        | "local_binding_changed"
        | "local_binding_unavailable"
        | "local_claim_unavailable"
        | "local_instance_conflict"
        | "local_runtime_too_old"
        | "local_runtime_upgrade_required"
        | "name_taken"
        | "pairing_recovery_required"
        | "rate_limited"
        | "subject_mismatch" => ClientError::new(code),
        _ => invalid(),
    }
}

async fn read<R: AsyncBufRead + Unpin>(reader: &mut R) -> Result<Frame> {
    let mut bytes = Vec::new();
    loop {
        let buf = reader.fill_buf().await.map_err(|_| invalid())?;
        let end = buf.iter().position(|b| *b == b'\n');
        let n = end.map_or(buf.len(), |i| i + 1);
        if n == 0 || bytes.len() + n > MAX_FRAME {
            return Err(invalid());
        }
        bytes.extend_from_slice(&buf[..n]);
        reader.consume(n);
        if end.is_some() {
            return serde_json::from_slice(&bytes).map_err(|_| invalid());
        }
    }
}
async fn write<W: AsyncWrite + Unpin>(writer: &mut W, body: Value) -> Result<()> {
    let mut data = serde_json::to_vec(&body).map_err(|_| invalid())?;
    data.push(b'\n');
    if data.len() > MAX_FRAME {
        return Err(invalid());
    }
    writer.write_all(&data).await.map_err(|_| invalid())?;
    writer.flush().await.map_err(|_| invalid())
}

fn nonce(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

// Both modes start on the private pipes of the adopted executable. The origin
// is never supplied by a page; the one HTTP success is not a machine frame.
async fn admit<R: AsyncBufRead + Unpin, W: AsyncWrite + Unpin>(
    output: &mut R,
    input: &mut W,
    origin: &LoopbackOrigin,
    transport: Arc<dyn Transport>,
    hello: Value,
) -> Result<()> {
    timeout(Duration::from_secs(15), async {
        // A CLI that predates the pipe exits on its unknown command: the hello
        // cannot be written, or no first frame comes. Only the Runtime's own
        // missing route turns that into "too old".
        let first = async {
            write(input, hello).await?;
            read(output).await
        }
        .await;
        let challenge = match first {
            Ok(Frame::Challenge { v: 1, challenge }) => challenge,
            Ok(_) => return Err(invalid()),
            Err(error) => {
                return Err(if claim_protocol_missing(origin, transport).await {
                    too_old()
                } else {
                    error
                })
            }
        };
        if !nonce(&challenge) {
            return Err(invalid());
        }
        let mut local = NativeSession::new(origin.navigation_url(), transport);
        let reply = local_mutation(&mut local, ADMIT, json!({"challenge":challenge}))
            .await
            .map_err(|_| ClientError::new("local_admission_failed"))?;
        if reply.status != 204 || !reply.body.is_empty() {
            return Err(if reply.status == 404 {
                too_old()
            } else {
                ClientError::new("local_admission_failed")
            });
        }
        write(input, json!({"v":1,"op":"begin","challenge":challenge})).await
    })
    .await
    .map_err(|_| expired())?
}

struct ArmRequest {
    claim_id: String,
    hostname: String,
    existing_instance_id: Option<String>,
    epochs: [Epoch; 2],
}
impl ArmRequest {
    fn live_value(self) -> Result<Value> {
        for epoch in self.epochs {
            epoch.check()?;
        }
        Ok(json!({"v":1,"op":"arm","claim_id":self.claim_id,
            "kind":if self.existing_instance_id.is_some() {"existing"} else {"new"},
            "hostname":self.hostname,"existing_instance_id":self.existing_instance_id}))
    }
}
impl MachineTicket {
    fn arm(&self, subject: &str, grant: ReviewedGrant) -> Result<ArmRequest> {
        if Instant::now() >= self.deadline {
            return Err(expired());
        }
        let (claim_id, expected_subject, hostname, existing_instance_id, epochs) = grant.into_parts();
        let prefix = hostname.strip_suffix(".avibe.bot").unwrap_or("");
        if claim_id != self.claim_id
            || expected_subject != subject
            || prefix.is_empty()
            || prefix.len() > 63
            || prefix.starts_with('-')
            || prefix.ends_with('-')
            || !prefix
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            || existing_instance_id.as_deref().is_some_and(|s| !text(s, 120))
        {
            return Err(ClientError::new("decision_changed"));
        }
        if existing_instance_id.is_some() && !self.existing_readback {
            return Err(ClientError::new("local_runtime_upgrade_required"));
        }
        Ok(ArmRequest {
            epochs,
            claim_id,
            hostname,
            existing_instance_id,
        })
    }
}

/// Owned only by native account orchestration. Dropping the attempt (including
/// its opening/arming future), its document guard, or the adoption closes stdin.
pub(crate) struct MachineAttempt {
    ticket: MachineTicket,
    subject: String,
    locale: String,
    adoption: watch::Receiver<bool>,
    arm: oneshot::Sender<ArmRequest>,
    result: oneshot::Receiver<Result<MachineOutcome>>,
}
impl MachineAttempt {
    fn check_live(&self) -> Result<()> {
        if !*self.adoption.borrow() || !matches!(self.adoption.has_changed(), Ok(false)) || self.arm.is_closed() {
            return Err(retired());
        }
        if Instant::now() >= self.ticket.deadline {
            return Err(expired());
        }
        Ok(())
    }
    pub(crate) async fn open(
        host: &RuntimeHost,
        transport: Arc<dyn Transport>,
        subject: String,
        locale: &str,
        document_retired: oneshot::Receiver<()>,
    ) -> Result<Self> {
        if !text(&subject, 200) || !matches!(locale, "en" | "zh") {
            return Err(invalid());
        }
        let source = host
            .claim_source()
            .ok_or_else(|| ClientError::new("local_runtime_upgrade_required"))?;
        Self::open_source(source, transport, subject, locale, document_retired).await
    }

    async fn open_source(
        source: ClaimSource,
        transport: Arc<dyn Transport>,
        subject: String,
        locale: &str,
        mut document_retired: oneshot::Receiver<()>,
    ) -> Result<Self> {
        let ClaimSource {
            command,
            origin,
            mut live,
        } = source;
        if !*live.borrow() || !matches!(document_retired.try_recv(), Err(oneshot::error::TryRecvError::Empty)) {
            return Err(retired());
        }
        let mut child = Command::from(command.0)
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| ClientError::new("local_runtime_upgrade_required"))?;
        let mut input = child.stdin.take().ok_or_else(invalid)?;
        let mut output = BufReader::new(child.stdout.take().ok_or_else(invalid)?);
        let adoption = live.clone();
        let (tickets, ticket_rx) = oneshot::channel();
        let (arm, decision) = oneshot::channel::<ArmRequest>();
        let (mut finish, result) = oneshot::channel();
        let open = json!({"v":1,"op":"open","subject":subject,"origin":origin.as_str(),"locale":locale});
        tokio::spawn(async move {
            let work = async {
                admit(&mut output, &mut input, &origin, transport, open).await?;
                let machine = timeout(Duration::from_secs(45), read(&mut output))
                    .await
                    .map_err(|_| expired())??
                    .ticket()?;
                let deadline = machine.deadline;
                let claim = machine.claim_id.clone();
                tickets.send(machine).map_err(|_| retired())?;
                // Before arm there is no legal success frame, partial frame, or EOF.
                // Peek without consuming so cancellation cannot discard a frame prefix.
                let value = timeout_at(deadline, async {
                    tokio::select! { biased;
                        _ = output.fill_buf() => Err(invalid()),
                        value = decision => value.map_err(|_| retired()),
                    }
                })
                .await
                .map_err(|_| expired())??;
                if Instant::now() >= deadline {
                    return Err(expired());
                }
                // Once any arm bytes could have reached the worker, transport loss is
                // ambiguous. Never tell the caller that the grant or publication rolled back.
                timeout_at(deadline, async {
                    let existing = value.existing_instance_id.clone();
                    let arm = value.live_value()?;
                    write(&mut input, arm)
                        .await
                        .map_err(|_| ClientError::new("fresh_claim_required"))?;
                    match read(&mut output)
                        .await
                        .map_err(|_| ClientError::new("fresh_claim_required"))?
                        .outcome()?
                    {
                        Notice::Declined => Ok(None),
                        Notice::Finished => {
                            let until = deadline.min(Instant::now() + Duration::from_secs(45));
                            let sample = timeout_at(
                                until,
                                publication::Sample::original(&mut output, &mut input, &claim, existing.as_deref()),
                            )
                            .await
                            .map_err(|_| expired())??;
                            Ok(Some((sample, until)))
                        }
                    }
                })
                .await
                .map_err(|_| ClientError::new("fresh_claim_required"))?
            };
            let outcome = tokio::select! {biased;
                _ = live.changed() => Err(retired()),
                _ = &mut document_retired => Err(retired()),
                _ = finish.closed() => Err(retired()),
                outcome = work => outcome,
            };
            match outcome {
                Ok(Some((sample, until))) => {
                    sample
                        .serve(&mut output, &mut input, &mut live, &mut document_retired, until, finish)
                        .await
                }
                Ok(None) => {
                    let _ = finish.send(Ok(MachineOutcome::Declined));
                }
                Err(error) => {
                    let _ = finish.send(Err(error));
                }
            }
            drop(input); // EOF first, so the Runtime can decline or finish an exact sealed handoff.
            drop(output);
            if timeout(Duration::from_secs(3), child.wait()).await.is_err() {
                let _ = child.start_kill();
                let _ = timeout(Duration::from_secs(2), child.wait()).await;
            }
        });
        match ticket_rx.await {
            Ok(ticket) => Ok(Self {
                ticket,
                subject,
                locale: locale.to_owned(),
                adoption,
                arm,
                result,
            }),
            Err(_) => Err(result.await.ok().and_then(|r| r.err()).unwrap_or_else(invalid)),
        }
    }

    /// Enqueue exactly once, under the coordinator's short owner lock. Neither the
    /// pipe write nor waiting for the child is part of this critical section.
    fn dispatch(self, grant: ReviewedGrant) -> Result<MachineCompletion> {
        self.check_live()?;
        let value = self.ticket.arm(&self.subject, grant)?;
        for epoch in &value.epochs {
            epoch.check()?;
        }
        self.arm.send(value).map_err(|_| retired())?;
        Ok(MachineCompletion(self.result))
    }
}

/// Receive-only. No ticket, target or sender survives here to re-arm the helper.
pub(crate) struct MachineCompletion(oneshot::Receiver<Result<MachineOutcome>>);
impl MachineCompletion {
    pub(crate) async fn wait(self) -> Result<MachineOutcome> {
        self.0.await.map_err(|_| ClientError::new("fresh_claim_required"))?
    }
}
