//! TOTP setup does not enroll a method. Keep the token in the private claim
//! owner and release the display material once, to the originating document.
mod finish;
use super::*;
use crate::instance_client::{
    account::BindingBase,
    protocol::SecondFactorGate,
    transport::{FactorCookies, NativeSession},
};

// Neither this object nor its inner secrets implement Debug, Clone or Serialize.
pub(in crate::instance_client::coordinator::claims::proof) struct IssuedTotp {
    token: String,
    cookies: FactorCookies,
    material: Option<TotpMaterial>,
}
pub(in crate::instance_client::coordinator) struct TotpMaterial {
    pub(in crate::instance_client::coordinator) manual_key: String,
    // Data for a local QR encoder, never a navigation target or markup.
    pub(in crate::instance_client::coordinator) qr_payload: String,
}
fn credentials(inner: &Inner, a: &Account) -> Result<FactorCookies> {
    let flow = &inner.account;
    let state = &inner.state.account;
    if !BindingBase::OwedFactor.matches(a, flow, state)
        || state.gate != Some(SecondFactorGate::Enroll)
        || !state.methods.is_empty()
        || !["/api/two-factor/totp/setup", "/api/two-factor/totp"]
            .into_iter()
            .all(|path| flow.session.same_request_cookies("/api/mobile/session", path))
    {
        return Err(ClientError::new("invalid_action"));
    }
    flow.session.factor_cookies().ok_or_else(changed)
}
pub(in crate::instance_client::coordinator::claims) use finish::EnrolledReply;

impl IssuedTotp {
    pub(in crate::instance_client::coordinator::claims::proof) fn material_delivered(&self) -> bool {
        self.material.is_none()
    }
    async fn fetch(mut jar: NativeSession, cookies: FactorCookies, account: &Account) -> Result<Self> {
        let invalid = || ClientError::new("invalid_response");
        let r = jar
            .factor_post("/api/two-factor/totp/setup", json!({}), &cookies)
            .await
            .map_err(|_| ClientError::new("request_failed"))?;
        if r.status != 200 {
            return Err(ClientError::new(match r.status {
                429 => "rate_limited",
                _ => "request_failed",
            }));
        }
        // Do not copy or interpret upstream SVG or URI. They confer no authority
        // and are not necessary to render exactly the key the person can type.
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            token: String,
            secret: String,
            #[serde(rename = "uri")]
            _uri: serde::de::IgnoredAny,
            #[serde(rename = "qrSvg")]
            _qr: serde::de::IgnoredAny,
        }
        let Wire { token, secret, .. } = serde_json::from_slice::<Wire>(&r.body).map_err(|_| invalid())?;
        if token.is_empty()
            || token.len() > 4096
            || !token.bytes().all(|b| b.is_ascii_graphic())
            || secret.len() != 39
            || !secret
                .split(' ')
                .all(|g| g.len() == 4 && g.bytes().all(|b| b.is_ascii_uppercase() || (b'2'..=b'7').contains(&b)))
        {
            return Err(invalid());
        }
        let label = format!("avibe.bot:{}", account.email);
        let label: String = url::form_urlencoded::byte_serialize(label.as_bytes()).collect();
        let compact = secret.replace(' ', "");
        let qr_payload =
            format!("otpauth://totp/{label}?secret={compact}&issuer=avibe.bot&algorithm=SHA1&digits=6&period=30");
        Ok(Self {
            token,
            cookies,
            material: Some(TotpMaterial {
                manual_key: secret,
                qr_payload,
            }),
        })
    }
}

/// This receipt contains no secret. The preflight did the account/credential
/// checks; the role-locked delivery must not take `inner` or wait on a writer.
#[must_use]
pub(in crate::instance_client::coordinator) struct TotpSetupReply<'a> {
    c: &'a Coordinator,
    owner: FactorOwner,
    borrow: Borrow<'a>,
}
impl TotpSetupReply<'_> {
    pub(in crate::instance_client::coordinator) fn deliver(mut self) -> Result<(Uuid, TotpMaterial)> {
        let mut claims = self.owner.caller.owner.lock().expect("claim owner");
        Epoch::new(self.c.generation.clone(), self.owner.generation).check()?;
        claims.expire();
        self.owner.caller.check()?;
        let active = claims
            .active
            .as_mut()
            .filter(|a| a.receipt == self.owner.receipt)
            .ok_or_else(stale)?;
        let Stage::Proof(step) = &mut active.stage else {
            return Err(changed());
        };
        step.proof.check(&active.account)?;
        let Phase::TotpSetup {
            predecessor,
            until,
            setup,
        } = &mut step.phase
        else {
            return Err(changed());
        };
        let slot = self.c.binding.try_lock().map_err(|_| busy())?;
        if Instant::now() >= *until || *predecessor != self.owner.predecessor || slot.committed() != Some(*predecessor)
        {
            return Err(stale());
        }
        let material = setup.material.take().ok_or_else(changed)?;
        self.borrow.returned = true;
        Ok((step.release(self.owner.predecessor), material))
    }
}
impl Coordinator {
    pub(in crate::instance_client::coordinator) async fn prepare_claim_totp_setup<'a>(
        &'a self,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
    ) -> Result<TotpSetupReply<'a>> {
        let owner = FactorOwner {
            caller: caller.clone(),
            generation,
            receipt,
            predecessor,
        };
        let (wake, mut stopped) = oneshot::channel();
        let (mut jar, cookies, a, until) = owner.with(self, At::Submitted, |inner, a, step, _| {
            let cookies = credentials(inner, a)?;
            // The issuer's 15 minutes are an upper bound, not a local promise
            // of acceptance; never start this clock at reply or delivery.
            let until = step.proof.deadline().min(Instant::now() + Duration::from_secs(15 * 60));
            step.phase = Phase::IssuingTotp {
                predecessor,
                until,
                _interrupt: wake,
            };
            Ok((inner.account.session.clone(), cookies, a.clone(), until))
        })?;
        let borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        jar.fence(caller.epoch.clone());
        let setup = tokio::select! {biased;
            _ = &mut stopped => return Err(stale()),
            _ = tokio::time::sleep_until(until) => return Err(ClientError::new("expired")),
            r = IssuedTotp::fetch(jar,cookies,&a) => r?,
        };
        owner.with(self, At::IssuingTotp, |inner, a, step, _| {
            if credentials(inner, a)? != setup.cookies {
                return Err(changed());
            }
            step.phase = Phase::TotpSetup {
                predecessor,
                until,
                setup: Box::new(setup),
            };
            Ok(())
        })?;
        Ok(TotpSetupReply { c: self, owner, borrow })
    }
}
