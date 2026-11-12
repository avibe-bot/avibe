//! Native system passkey assertions. No assertion or challenge token enters JS.
use async_trait::async_trait;
use avibe_runtime_host::instance_client::{account::Passkeys, protocol::ClientError, transport::Epoch};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde_json::{json, Value};
use tauri::AppHandle;

pub struct NativePasskeys {
    app: AppHandle,
}
impl NativePasskeys {
    pub fn new(app: AppHandle) -> Self {
        Self { app }
    }
}
struct Challenge {
    challenge: Vec<u8>,
    allowed: Vec<Vec<u8>>,
}
fn decode(value: &Value) -> Result<Vec<u8>, ClientError> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() < 16_384)
        .and_then(|s| URL_SAFE_NO_PAD.decode(s).ok())
        .ok_or_else(|| ClientError::new("invalid_passkey_challenge"))
}
fn challenge(options: &Value) -> Result<Challenge, ClientError> {
    if options["rpId"] != "avibe.bot" {
        return Err(ClientError::new("invalid_passkey_challenge"));
    }
    let mut allowed = vec![];
    if let Some(value) = options.get("allowCredentials") {
        let descriptors = value
            .as_array()
            .filter(|v| v.len() <= 100)
            .ok_or_else(|| ClientError::new("invalid_passkey_challenge"))?;
        for descriptor in descriptors {
            if descriptor["type"] != "public-key" {
                return Err(ClientError::new("invalid_passkey_challenge"));
            }
            allowed.push(decode(&descriptor["id"])?);
        }
    }
    Ok(Challenge {
        challenge: decode(&options["challenge"])?,
        allowed,
    })
}
fn assertion(id: &[u8], client: &[u8], authenticator: &[u8], signature: &[u8], user: &[u8]) -> Value {
    let mut response = json!({"clientDataJSON":URL_SAFE_NO_PAD.encode(client),"authenticatorData":URL_SAFE_NO_PAD.encode(authenticator),"signature":URL_SAFE_NO_PAD.encode(signature)});
    if !user.is_empty() {
        response["userHandle"] = URL_SAFE_NO_PAD.encode(user).into();
    }
    json!({"id":URL_SAFE_NO_PAD.encode(id),"rawId":URL_SAFE_NO_PAD.encode(id),"type":"public-key","response":response,"clientExtensionResults":{}})
}
#[async_trait]
impl Passkeys for NativePasskeys {
    async fn assert(&self, options: Value, epoch: Epoch) -> Result<Value, ClientError> {
        epoch.check()?;
        let input = challenge(&options)?;
        #[cfg(target_os = "macos")]
        {
            macos::perform(&self.app, input, epoch).await
        }
        #[cfg(windows)]
        {
            windows_native::perform(&self.app, input, epoch).await
        }
        #[cfg(not(any(target_os = "macos", windows)))]
        {
            let _ = input;
            Err(ClientError::new("passkey_unavailable"))
        }
    }
}
pub(crate) fn cancel(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.run_on_main_thread(macos::cancel);
    #[cfg(windows)]
    windows_native::cancel(app);
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    use objc2::{
        define_class, msg_send,
        rc::Retained,
        runtime::{NSObject, ProtocolObject},
        AnyThread, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly,
    };
    use objc2_app_kit::NSWindow;
    use objc2_authentication_services::*;
    use objc2_foundation::{NSArray, NSData, NSError, NSObjectProtocol, NSString};
    use std::cell::RefCell;
    use tauri::Manager;
    use tokio::sync::oneshot;

    struct Fields {
        anchor: Retained<NSWindow>,
    }
    define_class!(
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[ivars = Fields]
        struct Delegate;
        unsafe impl NSObjectProtocol for Delegate {}
        unsafe impl ASAuthorizationControllerDelegate for Delegate {
            #[unsafe(method(authorizationController:didCompleteWithAuthorization:))]
            fn completed(&self, controller: &ASAuthorizationController, auth: &ASAuthorization) {
                let result = unsafe {
                    let credential = auth.credential();
                    let object: &objc2::runtime::AnyObject = credential.as_ref();
                    let value = object.downcast_ref::<ASAuthorizationPlatformPublicKeyCredentialAssertion>();
                    value
                        .map(|value| {
                            assertion(
                                &value.credentialID().to_vec(),
                                &value.rawClientDataJSON().to_vec(),
                                &value.rawAuthenticatorData().to_vec(),
                                &value.signature().to_vec(),
                                &value.userID().to_vec(),
                            )
                        })
                        .ok_or_else(|| ClientError::new("passkey_unavailable"))
                };
                finish(controller, result);
            }
            #[unsafe(method(authorizationController:didCompleteWithError:))]
            fn failed(&self, controller: &ASAuthorizationController, error: &NSError) {
                let code = if error.domain().to_string() == "com.apple.AuthenticationServices.AuthorizationError"
                    && error.code() == 1001
                {
                    "passkey_cancelled"
                } else {
                    "passkey_unavailable"
                };
                finish(controller, Err(ClientError::new(code)));
            }
        }
        unsafe impl ASAuthorizationControllerPresentationContextProviding for Delegate {
            #[unsafe(method_id(presentationAnchorForAuthorizationController:))]
            fn anchor(&self, _: &ASAuthorizationController) -> Retained<NSWindow> {
                self.ivars().anchor.clone()
            }
        }
    );
    struct Pending {
        controller: Retained<ASAuthorizationController>,
        _delegate: Retained<Delegate>,
        epoch: Epoch,
        reply: oneshot::Sender<Result<Value, ClientError>>,
    }
    thread_local! { static PENDING: RefCell<Option<Pending>> = const { RefCell::new(None) }; }
    fn finish(controller: &ASAuthorizationController, result: Result<Value, ClientError>) {
        let pending = PENDING.with(|slot| {
            let mut slot = slot.borrow_mut();
            if slot.as_ref().is_some_and(|p| std::ptr::eq(&*p.controller, controller)) {
                slot.take()
            } else {
                None
            }
        });
        if let Some(pending) = pending {
            let _ = pending.reply.send(pending.epoch.check().and(result));
        }
    }
    pub fn cancel() {
        let pending = PENDING.with(|slot| slot.borrow_mut().take());
        if let Some(pending) = pending {
            unsafe {
                pending.controller.cancel();
            }
            let _ = pending.reply.send(Err(ClientError::new("passkey_cancelled")));
        }
    }
    pub async fn perform(app: &AppHandle, input: Challenge, epoch: Epoch) -> Result<Value, ClientError> {
        let (tx, rx) = oneshot::channel();
        let app = app.clone();
        let cancel_app = app.clone();
        app.clone()
            .run_on_main_thread(move || {
                let result = (|| {
                    epoch.check()?;
                    if PENDING.with(|slot| slot.borrow().is_some()) {
                        return Err(ClientError::new("busy"));
                    }
                    let mtm = MainThreadMarker::new().ok_or_else(|| ClientError::new("passkey_unavailable"))?;
                    let window = app
                        .get_webview_window(crate::MAIN_WINDOW)
                        .ok_or_else(|| ClientError::new("passkey_unavailable"))?;
                    // SAFETY: this runs on the UI thread and Tauri owns this NSWindow.
                    unsafe {
                        let anchor = Retained::retain(
                            window
                                .ns_window()
                                .map_err(|_| ClientError::new("passkey_unavailable"))?
                                .cast::<NSWindow>(),
                        )
                        .ok_or_else(|| ClientError::new("passkey_unavailable"))?;
                        let provider =
                            ASAuthorizationPlatformPublicKeyCredentialProvider::initWithRelyingPartyIdentifier(
                                ASAuthorizationPlatformPublicKeyCredentialProvider::alloc(),
                                &NSString::from_str("avibe.bot"),
                            );
                        let request = provider
                            .createCredentialAssertionRequestWithChallenge(&NSData::with_bytes(&input.challenge));
                        let descriptors: Vec<_> = input
                            .allowed
                            .iter()
                            .map(|id| {
                                ASAuthorizationPlatformPublicKeyCredentialDescriptor::initWithCredentialID(
                                    ASAuthorizationPlatformPublicKeyCredentialDescriptor::alloc(),
                                    &NSData::with_bytes(id),
                                )
                            })
                            .collect();
                        request.setAllowedCredentials(&NSArray::from_retained_slice(&descriptors));
                        request.setUserVerificationPreference(
                            ASAuthorizationPublicKeyCredentialUserVerificationPreferenceRequired
                                .ok_or_else(|| ClientError::new("passkey_unavailable"))?,
                        );
                        let requests: Retained<NSArray<ASAuthorizationRequest>> =
                            NSArray::from_slice(&[request.as_super()]);
                        let controller = ASAuthorizationController::initWithAuthorizationRequests(
                            ASAuthorizationController::alloc(),
                            &requests,
                        );
                        let delegate = mtm.alloc::<Delegate>().set_ivars(Fields { anchor });
                        let delegate: Retained<Delegate> = msg_send![super(delegate), init];
                        controller.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
                        controller.setPresentationContextProvider(Some(ProtocolObject::from_ref(&*delegate)));
                        Ok((controller, delegate))
                    }
                })();
                match result {
                    Ok((controller, delegate)) => {
                        PENDING.with(|slot| {
                            *slot.borrow_mut() = Some(Pending {
                                controller: controller.clone(),
                                _delegate: delegate,
                                epoch,
                                reply: tx,
                            })
                        });
                        unsafe {
                            controller.performRequests();
                        }
                    }
                    Err(error) => {
                        let _ = tx.send(Err(error));
                    }
                }
            })
            .map_err(|_| ClientError::new("passkey_unavailable"))?;
        match tokio::time::timeout(std::time::Duration::from_secs(120), rx).await {
            Ok(Ok(result)) => result,
            _ => {
                cancel_on_main(&cancel_app);
                Err(ClientError::new("passkey_cancelled"))
            }
        }
    }
    fn cancel_on_main(app: &AppHandle) {
        let _ = app.run_on_main_thread(cancel);
    }
}

#[cfg(windows)]
#[path = "instance_passkeys_windows.rs"]
mod windows_native;
