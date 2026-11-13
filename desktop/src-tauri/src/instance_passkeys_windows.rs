//! Windows WebAuthn system UI, scoped to the current account generation.
use super::*;
use std::sync::Mutex;
use tauri::Manager;
use windows::{
    core::{w, GUID},
    Win32::{Foundation::HWND, Networking::WindowsWebServices::*},
};
static PENDING: Mutex<Option<GUID>> = Mutex::new(None);

pub fn cancel(_: &AppHandle) {
    if let Some(id) = *PENDING.lock().expect("passkey operation") {
        // This API cancels only the exact native operation ID we allocated.
        let _ = unsafe { WebAuthNCancelCurrentOperation(&id) };
    }
}
pub async fn perform(app: &AppHandle, input: Challenge, epoch: Epoch) -> Result<Value, ClientError> {
    let window = app
        .get_webview_window(crate::MAIN_WINDOW)
        .ok_or_else(|| ClientError::new("passkey_unavailable"))?;
    let handle = window.hwnd().map_err(|_| ClientError::new("passkey_unavailable"))?.0 as usize;
    tokio::task::spawn_blocking(move || {
        epoch.check()?;
        // SAFETY: buffers live through this blocking native call. The API owns
        // the assertion allocation until WebAuthNFreeAssertion below.
        unsafe {
            let mut id = WebAuthNGetCancellationId().map_err(|_| ClientError::new("passkey_unavailable"))?;
            {
                let mut pending = PENDING.lock().expect("passkey operation");
                if pending.is_some() { return Err(ClientError::new("busy")); }
                *pending = Some(id);
            }
            struct Cleanup;
            impl Drop for Cleanup { fn drop(&mut self) { *PENDING.lock().expect("passkey operation") = None; } }
            let _cleanup = Cleanup;
            epoch.check()?;
            let mut data = serde_json::to_vec(&json!({"type":"webauthn.get","challenge":URL_SAFE_NO_PAD.encode(&input.challenge),"origin":"https://avibe.bot","crossOrigin":false})).expect("client JSON");
            let client = WEBAUTHN_CLIENT_DATA { dwVersion: 1, cbClientDataJSON: data.len() as u32, pbClientDataJSON: data.as_mut_ptr(), pwszHashAlgId: WEBAUTHN_HASH_ALGORITHM_SHA_256 };
            let mut allowed = input.allowed;
            let mut credentials: Vec<_> = allowed.iter_mut().map(|id| WEBAUTHN_CREDENTIAL { dwVersion:1, cbId:id.len() as u32, pbId:id.as_mut_ptr(), pwszCredentialType:w!("public-key") }).collect();
            let options = WEBAUTHN_AUTHENTICATOR_GET_ASSERTION_OPTIONS {
                dwVersion: WEBAUTHN_AUTHENTICATOR_GET_ASSERTION_OPTIONS_VERSION_3,
                dwTimeoutMilliseconds: 120_000,
                CredentialList: WEBAUTHN_CREDENTIALS { cCredentials:credentials.len() as u32, pCredentials:credentials.as_mut_ptr() },
                dwUserVerificationRequirement: WEBAUTHN_USER_VERIFICATION_REQUIREMENT_REQUIRED,
                pCancellationId: &mut id,
                ..Default::default()
            };
            let raw = WebAuthNAuthenticatorGetAssertion(HWND(handle as *mut _), w!("avibe.bot"), &client, Some(&options))
                .map_err(|_| ClientError::new("passkey_cancelled"))?;
            struct Owned(*mut WEBAUTHN_ASSERTION);
            impl Drop for Owned { fn drop(&mut self) { unsafe { WebAuthNFreeAssertion(self.0); } } }
            let owned = Owned(raw);
            epoch.check()?;
            let result = owned.0.as_ref().ok_or_else(|| ClientError::new("passkey_unavailable"))?;
            fn bytes<'a>(ptr: *const u8, len: u32) -> Result<&'a [u8], ClientError> {
                if len == 0 { return Ok(&[]); }
                if ptr.is_null() || len > 2_000_000 { return Err(ClientError::new("passkey_unavailable")); }
                // SAFETY: only Windows-owned assertion fields reach this helper,
                // and the allocation remains alive until the output is copied.
                Ok(unsafe { std::slice::from_raw_parts(ptr, len as usize) })
            }
            Ok(assertion(bytes(result.Credential.pbId,result.Credential.cbId)?, &data,
                bytes(result.pbAuthenticatorData,result.cbAuthenticatorData)?, bytes(result.pbSignature,result.cbSignature)?, bytes(result.pbUserId,result.cbUserId)?))
        }
    }).await.map_err(|_| ClientError::new("passkey_unavailable"))?
}
