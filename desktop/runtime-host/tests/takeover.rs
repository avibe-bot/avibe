//! An updated bundled app must not silently navigate to an old local service.
//! Existing bootstrap fixtures explicitly allow external adoption; these cover
//! the production management policy and its confirmation boundary.
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;

use async_trait::async_trait;
use avibe_runtime_host::takeover::ExternalRuntime;
use avibe_runtime_host::{
    BootstrapNoticeCode, DiscardStatus, HealthProbe, LaunchError, LaunchWatch, LaunchedRuntime, LoopbackOrigin,
    ResolvedRuntimeLauncher, RuntimeHost, RuntimeHostSettings, RuntimeLauncher, RuntimeReadiness,
};

#[derive(Clone, Default)]
struct Instance {
    independent: Arc<AtomicBool>,
    managed: Arc<AtomicBool>,
    starts: Arc<AtomicUsize>,
    stops: Arc<AtomicUsize>,
    wrong_home: Arc<AtomicBool>,
    refuse: bool,
    race: bool,
    unavailable: bool,
    other_desktop: bool,
}

impl RuntimeLauncher for Instance {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        Ok(Arc::new(self.clone()))
    }
}

impl ResolvedRuntimeLauncher for Instance {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
        Ok(LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap())
    }
    fn launch(&self) -> Result<LaunchedRuntime, LaunchError> {
        self.starts.fetch_add(1, Ordering::SeqCst);
        Ok(LaunchedRuntime {
            pid: 1,
            watch: LaunchWatch::exited(true),
        })
    }
    fn expected_runtime_id(&self) -> Option<&str> {
        Some("bundled")
    }
    fn handover(&self) -> Result<(), LaunchError> {
        panic!("an independent connection cannot hand over its service")
    }
    fn prune_superseded(&self) {
        assert!(
            !self.independent.load(Ordering::SeqCst),
            "an independent connection cannot prune installations"
        );
    }
    fn external_runtime(&self) -> Option<ExternalRuntime> {
        (!self.managed.load(Ordering::SeqCst) && !self.race).then(|| ExternalRuntime {
            home: "/tmp/已有数据".into(),
            service: None,
            ui: None,
            reason: None,
        })
    }
    fn allows_external(&self) -> bool {
        self.independent.load(Ordering::SeqCst)
    }
    fn connect_only(&self) -> bool {
        self.independent.load(Ordering::SeqCst)
    }
    fn verify_independent_connection(&self) -> bool {
        !self.wrong_home.load(Ordering::SeqCst)
    }
    fn choose_management(&self, takeover: bool) -> Result<(), LaunchError> {
        if self.refuse {
            return Err(LaunchError::TakeoverRefused);
        }
        if takeover {
            self.stops.fetch_add(1, Ordering::SeqCst);
            self.managed.store(true, Ordering::SeqCst);
        } else {
            self.independent.store(true, Ordering::SeqCst);
        }
        Ok(())
    }
}

#[async_trait]
impl HealthProbe for Instance {
    async fn readiness(&self, _origin: &LoopbackOrigin) -> Option<RuntimeReadiness> {
        if self.unavailable {
            return None;
        }
        if self.managed.load(Ordering::SeqCst) && self.starts.load(Ordering::SeqCst) == 0 {
            return None;
        }
        Some(RuntimeReadiness {
            desktop_runtime_id: if self.other_desktop {
                Some("other-desktop".into())
            } else {
                self.managed.load(Ordering::SeqCst).then(|| "bundled".into())
            },
            desktop_ui_runtime_id: None,
        })
    }
}

fn host(instance: &Instance) -> RuntimeHost {
    RuntimeHost::new(
        Arc::new(instance.clone()),
        Arc::new(instance.clone()),
        RuntimeHostSettings {
            poll_interval: Duration::from_millis(1),
            ..RuntimeHostSettings::default()
        },
    )
}

#[tokio::test]
async fn confirmed_takeover_is_required_before_a_bundled_bootstrap_can_serve_the_instance() {
    for independent in [false, true] {
        let instance = Instance::default();
        let host = host(&instance);
        assert_eq!(
            host.bootstrap(&DiscardStatus).await.notice.code,
            BootstrapNoticeCode::TakeoverRequired
        );
        assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
        assert_eq!(instance.stops.load(Ordering::SeqCst), 0);
        assert!(
            !host.manages_connection(),
            "a pending decision is not a managed connection"
        );
        host.choose_management(!independent).await.unwrap();
        assert!(host.bootstrap(&DiscardStatus).await.phase == avibe_runtime_host::BootstrapPhase::Ready);
        assert_eq!(host.manages_connection(), !independent);
        assert_eq!(instance.stops.load(Ordering::SeqCst), usize::from(!independent));
        assert_eq!(instance.starts.load(Ordering::SeqCst), usize::from(!independent));
        if !independent {
            instance.managed.store(false, Ordering::SeqCst);
            let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap();
            assert!(
                !host.is_ready(&origin).await,
                "an external replacement is not the verified successor"
            );
        }
    }
}

#[tokio::test]
async fn an_external_service_appearing_after_discovery_cannot_bypass_the_choice() {
    let instance = Instance {
        race: true,
        ..Instance::default()
    };
    assert_eq!(
        host(&instance).bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::TakeoverRequired
    );
    assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn failed_takeover_does_not_start_or_adopt_the_predecessor() {
    let instance = Instance {
        refuse: true,
        ..Instance::default()
    };
    let host = host(&instance);
    host.bootstrap(&DiscardStatus).await;
    assert!(host.choose_management(true).await.is_err());
    assert_eq!(
        host.bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::TakeoverRequired
    );
    assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn independent_mode_does_not_start_a_bundled_replacement_when_its_service_is_down() {
    let instance = Instance {
        unavailable: true,
        ..Instance::default()
    };
    instance.independent.store(true, Ordering::SeqCst);
    assert_eq!(
        host(&instance).bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::IndependentUnavailable
    );
    assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
    assert_eq!(instance.stops.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn independent_mode_does_not_replace_another_desktop_version() {
    let instance = Instance {
        other_desktop: true,
        ..Instance::default()
    };
    instance.independent.store(true, Ordering::SeqCst);
    assert_eq!(
        host(&instance).bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::Adopted
    );
    assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
    assert_eq!(instance.stops.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn independent_readiness_cannot_adopt_or_monitor_a_different_home_at_the_same_origin() {
    let instance = Instance::default();
    instance.independent.store(true, Ordering::SeqCst);
    let host = host(&instance);
    assert_eq!(
        host.bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::Adopted
    );
    let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap();
    assert!(host.is_ready(&origin).await);
    instance.wrong_home.store(true, Ordering::SeqCst);
    assert!(!host.is_ready(&origin).await);
    assert_eq!(
        host.bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::IndependentUnavailable
    );
    assert_eq!(instance.starts.load(Ordering::SeqCst), 0);
    assert_eq!(instance.stops.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn adopted_managed_connections_are_reported_only_after_verified_readiness() {
    let instance = Instance::default();
    instance.managed.store(true, Ordering::SeqCst);
    instance.starts.store(1, Ordering::SeqCst);
    let host = host(&instance);
    assert!(!host.manages_connection());
    assert_eq!(
        host.bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::Adopted
    );
    assert!(
        !host.has_owned_runtime(),
        "an adopted connection grants no stop receipt"
    );
    assert!(host.manages_connection());
    instance.managed.store(false, Ordering::SeqCst);
    assert_eq!(
        host.bootstrap(&DiscardStatus).await.notice.code,
        BootstrapNoticeCode::TakeoverRequired
    );
    assert!(!host.manages_connection());
}
