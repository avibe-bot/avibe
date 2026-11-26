//! "Upgrade and try again": upgrades this computer's Runtime when it is too old
//! for this Desktop (`local_runtime_too_old`), with the same loopback calls the
//! Workbench's own Version & update section makes on it, then waits until this
//! Desktop has the upgraded Runtime again. Nothing is reimplemented natively:
//! the Runtime installs and restarts itself (`POST /api/upgrade`).
//!
//! The page repeats the step that needed the newer Runtime (looking at this
//! computer's binding, or naming it) once this answers `upgraded`: that step's
//! document admission and receipts are its own, and are never replayed here.
use super::*;
use crate::{instance_client::enrollment::local_mutation, instance_client::machine, LoopbackOrigin, RuntimeHost};
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::{sleep, Instant};

/// How long the Runtime may take to install, restart and be this Desktop's
/// again after it accepted the upgrade.
pub(super) const UPGRADE_WAIT: Duration = Duration::from_secs(5 * 60);
const POLL: Duration = Duration::from_secs(2);

#[cfg(test)]
mod tests;

fn failed() -> ClientError {
    ClientError::new("local_runtime_upgrade_failed")
}

/// Clears `upgrading` however the upgrade ends.
struct Upgrading<'a>(&'a AtomicBool);
impl Drop for Upgrading<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

impl Coordinator {
    /// The shell's `upgrade_local_runtime`. Returns `upgraded` or `desktop_update`
    /// (`DirectResult::LocalRuntimeUpgrade`), or `local_runtime_upgrade_failed`,
    /// `local_runtime_upgrade_timed_out` or `local_runtime_still_too_old`.
    pub async fn upgrade_local_runtime(&self, host: &RuntimeHost) -> Result<DirectResult, ClientError> {
        if self.signing_out.load(Ordering::SeqCst) || self.upgrading.swap(true, Ordering::SeqCst) {
            return Err(ClientError::new("busy"));
        }
        let _upgrading = Upgrading(&self.upgrading);
        let origin = self
            .inner
            .lock()
            .expect("client state")
            .local
            .as_ref()
            .map(|local| local.origin.clone())
            .ok_or_else(|| ClientError::new("local_runtime_unavailable"))?;
        let loopback = LoopbackOrigin::parse(origin.as_str()).map_err(|_| failed())?;
        let session = || NativeSession::new(origin.clone(), self.transport.clone());
        let state = |state: &str| DirectResult::LocalRuntimeUpgrade { state: state.into() };

        let mut version = self.local_version(&mut session()).await.ok_or_else(failed)?;
        if version["managed_by"] == "desktop" {
            return Ok(state("desktop_update"));
        }
        // Nothing to upgrade: the step may simply be tried again. A probe that
        // cannot tell neither skips nor starts an upgrade.
        match self.local_runtime_ready(host, &loopback).await {
            Some(true) => {
                self.inner.lock().expect("client state").claim_protocol_missing = false;
                self.refresh_local().await;
                return Ok(state("upgraded"));
            }
            None => return Err(failed()),
            Some(false) => {}
        }
        // A prerelease Desktop needs a prerelease Runtime. Its user already takes
        // prereleases of this app, so the Runtime's switch is turned on and left on.
        if self.desktop_prerelease() && version["prereleases"] != true {
            let reply = local_mutation(&mut session(), "/api/update/prereleases", json!({"enabled": true}))
                .await
                .map_err(|_| failed())?;
            if !reply.success() {
                return Err(failed());
            }
            version = self.local_version(&mut session()).await.ok_or_else(failed)?;
        }
        // The user asked for this upgrade: read the offer as the Workbench's
        // check does, which also finds a silent release (docs/release.md) the
        // unprompted version read leaves out. A Runtime without the route has
        // only that read.
        if let Some(checked) = self.local_check(&mut session()).await {
            version = checked;
        }
        if version["has_update"] != true {
            return Err(ClientError::new("local_runtime_still_too_old"));
        }
        let before = version["current"].as_str().unwrap_or_default().to_owned();
        // The released endpoint: no `confirm_interrupt`, exactly as an older
        // Workbench sends it, naming the offer so a Runtime that knows the field
        // installs exactly it (an older one ignores it). Its reply can be lost
        // to the restart it starts.
        let body = match version["latest"].as_str() {
            Some(latest) => json!({"target_version": latest}),
            None => json!({}),
        };
        match local_mutation(&mut session(), "/api/upgrade", body).await {
            Ok(reply) => {
                let body = reply.json().unwrap_or(Value::Null);
                if body["code"] == "desktop_managed_runtime" {
                    return Ok(state("desktop_update"));
                }
                if !reply.success() || body["ok"] != true {
                    return Err(failed());
                }
            }
            Err(error) if error.code == "network_unavailable" => {}
            Err(_) => return Err(failed()),
        }
        let deadline = Instant::now() + UPGRADE_WAIT;
        let mut upgraded = false;
        while Instant::now() < deadline {
            sleep(POLL).await;
            if !upgraded {
                upgraded = self
                    .local_version(&mut session())
                    .await
                    .and_then(|v| v["current"].as_str().map(str::to_owned))
                    .is_some_and(|current| current != before);
                if !upgraded {
                    continue;
                }
            }
            // The new Runtime answers. It must also be this Desktop's again (an
            // adoption the claim pipe can use), and have the claim protocol.
            // A restart too short for the shell's recovery leaves the adoption
            // with a retired claim channel; this arms it again for the same Runtime.
            let Some(source) = host.live_claim_source().await else {
                continue;
            };
            // A restart long enough for the shell's monitor to lose the Runtime
            // unbinds it here, and the recovery binds it again once re-adopted;
            // each starts a new generation and retires claim documents. Answer
            // only once it is bound to the adopted origin again, so the page's
            // repeated step runs in the generation that lasts.
            let bound = self
                .inner
                .lock()
                .expect("client state")
                .local
                .as_ref()
                .is_some_and(|local| local.origin == source.origin.navigation_url());
            if !bound {
                continue;
            }
            // Only the Runtime's own answer ends the wait; an inconclusive probe
            // (restarting, no token, a server error) is asked again.
            match machine::claim_route(&source.origin, self.transport.clone()).await {
                Some(true) => {}
                Some(false) => return Err(ClientError::new("local_runtime_still_too_old")),
                None => continue,
            }
            self.inner.lock().expect("client state").claim_protocol_missing = false;
            self.refresh_local().await;
            return Ok(state("upgraded"));
        }
        Err(ClientError::new("local_runtime_upgrade_timed_out"))
    }

    /// The Runtime's `/api/version`, or `None` when it does not answer one.
    async fn local_version(&self, session: &mut NativeSession) -> Option<Value> {
        let reply = session.request(Method::GET, "/api/version", None).await.ok()?;
        reply.success().then(|| reply.json().ok()).flatten()
    }

    /// The snapshot the Runtime answers a check with, or `None` when it has no
    /// such route (before standalone checks) or the check fails.
    async fn local_check(&self, session: &mut NativeSession) -> Option<Value> {
        let reply = local_mutation(session, "/api/update/check", json!({})).await.ok()?;
        let snapshot = reply.success().then(|| reply.json().ok()).flatten()?["snapshot"].take();
        snapshot.is_object().then_some(snapshot)
    }

    /// Whether this computer's Runtime already does what this Desktop needs of
    /// it: it describes itself, is this Desktop's, and has the claim protocol.
    /// `None` when all else holds but the claim-route probe cannot tell.
    async fn local_runtime_ready(&self, host: &RuntimeHost, origin: &LoopbackOrigin) -> Option<bool> {
        let mut session = NativeSession::new(origin.navigation_url(), self.transport.clone());
        let described = session
            .request(Method::GET, "/api/desktop/local-instance", None)
            .await
            .is_ok_and(|reply| reply.success());
        if !described || host.live_claim_source().await.is_none() {
            return Some(false);
        }
        machine::claim_route(origin, self.transport.clone()).await
    }

    fn desktop_prerelease(&self) -> bool {
        self.inner
            .lock()
            .expect("client state")
            .state
            .desktop
            .version
            .contains('-')
    }

    /// A claim step found this computer's Runtime too old: say so on this
    /// computer's row until an upgrade or a newer Runtime clears it. The shell
    /// calls this with no presentation lock held: it publishes state.
    pub fn note_runtime_too_old(&self) {
        let state = {
            let mut inner = self.inner.lock().expect("client state");
            inner.claim_protocol_missing = true;
            if inner.state.local.runtime_too_old {
                return;
            }
            inner.state.local.runtime_too_old = true;
            inner.state.revision += 1;
            inner.state.clone()
        };
        self.view.changed(&state);
    }
}
