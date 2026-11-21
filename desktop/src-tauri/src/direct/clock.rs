//! Local admission time since boot. Unlike an awake-only timer, this clock
//! advances during suspend. It is never compared to the instance's wall clock.
use std::{sync::Arc, time::Duration};

#[derive(Clone)]
pub(super) struct Clock(pub(super) Arc<dyn Fn() -> Duration + Send + Sync>);
impl Clock {
    pub(super) fn now(&self) -> Duration {
        (self.0)()
    }
}
impl Default for Clock {
    fn default() -> Self {
        Self(Arc::new(boot_time))
    }
}
#[cfg(target_os = "macos")]
fn boot_time() -> Duration {
    extern "C" {
        fn clock_gettime_nsec_np(clock: u32) -> u64;
    }
    // Darwin CLOCK_MONOTONIC (6), not CLOCK_UPTIME_RAW used by Instant.
    Duration::from_nanos(unsafe { clock_gettime_nsec_np(6) })
}
#[cfg(windows)]
fn boot_time() -> Duration {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetTickCount64() -> u64;
    }
    Duration::from_millis(unsafe { GetTickCount64() })
}
#[cfg(not(any(target_os = "macos", windows)))]
fn boot_time() -> Duration {
    // Unsupported desktop platform: allocating a finite lease must fail.
    Duration::MAX
}
