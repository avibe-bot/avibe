//! Final enrollment has two independent outputs: an ordinary bound session
//! and optional, non-repeatable display material. Never persist the latter.
use super::*;
use crate::instance_client::protocol::TotpMethod;
use serde::{de::IgnoredAny, Deserialize};

#[derive(Default)]
pub(in crate::instance_client) enum EnrolledCodes {
    #[default]
    NotIssued,
    Unavailable,
    Codes(Vec<String>),
}
fn read_codes<'de, D: serde::Deserializer<'de>>(d: D) -> Result<EnrolledCodes, D::Error> {
    let codes = Vec::<String>::deserialize(d)?;
    const ALPHABET: &[u8] = b"abcdefghjkmnpqrstuvwxyz23456789";
    let valid = codes.len() == 10
        && codes.iter().enumerate().all(|(i, s)| {
            s.len() == 11
                && s.bytes()
                    .enumerate()
                    .all(|(i, b)| if i == 5 { b == b'-' } else { ALPHABET.contains(&b) })
                && !codes[..i].contains(s)
        });
    Ok(if valid {
        EnrolledCodes::Codes(codes)
    } else {
        EnrolledCodes::Unavailable
    })
}
impl BoundClaimSession {
    pub(in crate::instance_client) async fn enroll_totp(
        mut flow: Box<AccountFlow>,
        account: Account,
        cookies: &FactorCookies,
        token: String,
        code: String,
        epoch: Epoch,
    ) -> Result<(Self, EnrolledCodes), ClientError> {
        let unknown = || ClientError::new("factor_delivery_unknown");
        flow.fence(epoch);
        let r = flow
            .session
            .factor_post("/api/two-factor/totp", json!({"token":token,"code":code}), cookies)
            .await
            .map_err(|_| unknown())?;
        if r.status != 201 {
            let refusal = factor_refusal(&r);
            return Err(if refusal.code == "invalid_or_expired" {
                ClientError::new("invalid_code")
            } else {
                refusal
            });
        }
        // Only a coherent method receipt establishes acceptance. The method ID
        // is not an account witness; no returned method fields reach a page.
        #[derive(Deserialize)]
        struct Created {
            method: TotpMethod,
            #[serde(default)]
            error: Option<IgnoredAny>,
        }
        let created: Created = serde_json::from_slice(&r.body).map_err(|_| unknown())?;
        if created.error.is_some()
            || created.method.kind != "totp"
            || !created.method.id.strip_prefix("tfm_").is_some_and(|id| {
                !id.is_empty()
                    && id.len() <= 128
                    && id
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
            })
            || chrono::DateTime::parse_from_rfc3339(&created.method.created_at).is_err()
        {
            return Err(unknown());
        }
        // Decode this field independently: invalid/duplicate entries, an
        // explicit null or a malformed list do not un-enroll the method.
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Direct {
            #[serde(rename = "method")]
            _method: IgnoredAny,
            #[serde(default, rename = "recoveryCodes", deserialize_with = "read_codes")]
            codes: EnrolledCodes,
        }
        let codes = serde_json::from_slice::<Direct>(&r.body)
            .map(|x| x.codes)
            .unwrap_or(EnrolledCodes::Unavailable);
        Ok((
            Self {
                flow,
                account,
                base: BindingBase::OwedFactor,
            },
            codes,
        ))
    }
}
