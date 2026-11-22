//! Read-only continuation of the *armed* child. There is no operation here to start,
//! grant, choose a binding, or arm again. The first notification is not a receipt.
use super::*;
use crate::instance_client::{protocol::Account, transport::FactorCookies};

pub(super) struct Sample {
    instance: String,
    generation: String,
}
impl Sample {
    pub(super) async fn original<R: AsyncBufRead + Unpin, W: AsyncWrite + Unpin>(
        output: &mut R,
        input: &mut W,
        claim: &str,
        existing: Option<&str>,
    ) -> Result<Self> {
        write(input, json!({"v":1,"op":"observe_publication"})).await?;
        match read(output).await? {
            Frame::Publication {
                v: 1,
                claim_id,
                instance_id,
                generation,
            } if claim_id == claim
                && text(&instance_id, 120)
                && existing.is_none_or(|id| id == instance_id)
                && nonce(&generation) =>
            {
                Ok(Self {
                    instance: instance_id,
                    generation,
                })
            }
            other => Err(bound::frame_error(other)),
        }
    }
    pub(super) async fn serve<R: AsyncBufRead + Unpin, W: AsyncWrite + Unpin>(
        self,
        output: &mut R,
        input: &mut W,
        live: &mut watch::Receiver<bool>,
        stop: &mut oneshot::Receiver<()>,
        deadline: Instant,
        finish: oneshot::Sender<Result<MachineOutcome>>,
    ) {
        let (witness, receive, mut done) = Publication::channel(self.instance, live.clone(), deadline);
        if finish.send(Ok(MachineOutcome::Publication(witness))).is_err() {
            return;
        }
        let work = async {
            let challenge = tokio::select! { biased;
                _ = output.fill_buf() => return Err(invalid()),
                value = receive => value.map_err(|_| retired())?,
            };
            bound::echo(output, input, &self.generation, &challenge).await
        };
        let result = tokio::select! { biased;
            _ = live.changed() => Err(retired()),
            _ = stop => Err(retired()),
            _ = done.closed() => Err(retired()),
            _ = tokio::time::sleep_until(deadline) => Err(expired()),
            r = work => r,
        };
        let _ = done.send(result);
    }
}

pub(crate) struct Publication {
    instance: String,
    live: watch::Receiver<bool>,
    deadline: Instant,
    recheck: oneshot::Sender<String>,
    ack: oneshot::Receiver<Result<()>>,
}

/// Positive evidence is scoped to this adoption and bounded observation. Account
/// and exact confirming receipt are checked separately at the native delivery site.
pub(crate) struct ObservedOwner {
    live: watch::Receiver<bool>,
    deadline: Instant,
}
impl ObservedOwner {
    pub(crate) fn deadline(&self) -> Instant {
        self.deadline
    }
    pub(crate) fn check(&self) -> Result<()> {
        if Instant::now() >= self.deadline || !*self.live.borrow() || !matches!(self.live.has_changed(), Ok(false)) {
            return Err(retired());
        }
        Ok(())
    }
}
impl Publication {
    pub(super) fn channel(
        instance: String,
        live: watch::Receiver<bool>,
        deadline: Instant,
    ) -> (Self, oneshot::Receiver<String>, oneshot::Sender<Result<()>>) {
        let (recheck, receive) = oneshot::channel();
        let (done, ack) = oneshot::channel();
        (
            Self {
                instance,
                live,
                deadline,
                recheck,
                ack,
            },
            receive,
            done,
        )
    }
    pub(in crate::instance_client) async fn classify(
        self,
        mut jar: NativeSession,
        account: &Account,
        frozen: &FactorCookies,
    ) -> Result<ClassifiedOwner> {
        let Self {
            instance,
            live,
            deadline,
            recheck,
            ack,
        } = self;
        let stamp = ObservedOwner { live, deadline };
        let mut adoption = stamp.live.clone();
        stamp.check()?;
        let response = tokio::select! { biased;
            _ = adoption.changed() => return Err(retired()),
            _ = tokio::time::sleep_until(deadline) => return Err(expired()),
            r = jar.read_frozen_account(frozen) => r.map_err(|_| ClientError::new("account_observation_unavailable"))?,
        };
        if bound::catalog(response, account, &instance)? != bound::CurrentBinding::OwnerObserved {
            return Err(ClientError::new("binding_owner_unconfirmed"));
        }
        Ok(ClassifiedOwner { stamp, recheck, ack })
    }
}

pub(crate) struct ClassifiedOwner {
    stamp: ObservedOwner,
    recheck: oneshot::Sender<String>,
    ack: oneshot::Receiver<Result<()>>,
}
impl ClassifiedOwner {
    /// Synchronous enqueue under the coordinator's original-owner gate. The returned
    /// future has no sender, and waits for this one response outside all owner locks.
    pub(crate) fn dispatch(self) -> Result<impl std::future::Future<Output = Result<ObservedOwner>>> {
        let Self { stamp, recheck, ack } = self;
        stamp.check()?;
        recheck
            .send(uuid::Uuid::new_v4().simple().to_string())
            .map_err(|_| retired())?;
        Ok(async move {
            let mut live = stamp.live.clone();
            tokio::select! { biased;
                _ = live.changed() => return Err(retired()),
                _ = tokio::time::sleep_until(stamp.deadline) => return Err(expired()),
                r = ack => r.map_err(|_| retired())??,
            };
            stamp.check()?;
            Ok(stamp)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The coordinator tests own account classification. At the channel boundary,
    // ensure the original driver answers only a *new* same-generation recheck, or EOFs.
    #[tokio::test]
    async fn armed_driver_has_only_one_readback_and_releases_its_pipe() {
        for case in ["match", "early", "wrong_generation", "wrong_nonce", "drop"] {
            let (native, helper) = tokio::io::duplex(4096);
            let (reader, mut writer) = tokio::io::split(native);
            let (reader2, mut writer2) = tokio::io::split(helper);
            let mut output = BufReader::new(reader);
            let mut commands = BufReader::new(reader2).lines();
            let generation = "b".repeat(64);
            let challenge = "f".repeat(32);
            let response = |g: &str, c: &str| json!({"v":1,"event":"unchanged","generation":g,"challenge":c});
            if case == "early" {
                write(&mut writer2, response(&generation, &challenge)).await.unwrap();
            }
            let (_source, mut live) = watch::channel(true);
            let (_doc, mut stop) = oneshot::channel();
            let (finish, outcome) = oneshot::channel();
            let driver = async {
                let sample = Sample {
                    instance: "local".into(),
                    generation: generation.clone(),
                };
                sample
                    .serve(
                        &mut output,
                        &mut writer,
                        &mut live,
                        &mut stop,
                        Instant::now() + Duration::from_secs(45),
                        finish,
                    )
                    .await;
                drop(writer);
                drop(output);
            };
            let caller = async {
                let MachineOutcome::Publication(p) = outcome.await.unwrap().unwrap() else {
                    panic!("no sample")
                };
                if case == "drop" {
                    return;
                }
                let _ = p.recheck.send(challenge.clone());
                assert_eq!(p.ack.await.unwrap().is_ok(), case == "match");
            };
            let helper = async {
                if !matches!(case, "early" | "drop") {
                    let cmd: Value = serde_json::from_str(&commands.next_line().await.unwrap().unwrap()).unwrap();
                    assert_eq!(
                        cmd,
                        json!({"v":1,"op":"recheck","generation":generation,"challenge":challenge})
                    );
                    let reply = match case {
                        "wrong_generation" => response(&"a".repeat(64), &challenge),
                        "wrong_nonce" => response(&generation, &"0".repeat(32)),
                        _ => response(&generation, &challenge),
                    };
                    write(&mut writer2, reply).await.unwrap();
                }
                assert!(commands.next_line().await.unwrap().is_none());
            };
            timeout(Duration::from_secs(3), async { tokio::join!(driver, caller, helper) })
                .await
                .unwrap();
        }
    }
}
