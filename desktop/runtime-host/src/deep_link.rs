use std::sync::Arc;

use url::Url;

use crate::{BootstrapNoticeCode, BootstrapPhase, BootstrapStatus, LoopbackOrigin};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeepLinkTarget {
    path: String,
}

impl DeepLinkTarget {
    pub fn path(&self) -> &str {
        &self.path
    }

    pub fn navigation_url(&self, origin: &LoopbackOrigin) -> Url {
        origin
            .navigation_url()
            .join(&self.path)
            .expect("a parsed deep link is a same-origin path")
    }
}

pub fn parse_deep_link(raw: &str) -> Option<DeepLinkTarget> {
    let parsed = Url::parse(raw).ok()?;
    if parsed.scheme() != "avibe"
        || !raw.starts_with("avibe://")
        || parsed.as_str() != raw
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.port().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return None;
    }
    let path = match (parsed.host_str()?, parsed.path()) {
        ("session", path) | ("show", path) => {
            let identifier = path.strip_prefix('/')?;
            if !valid_identifier(identifier) {
                return None;
            }
            let prefix = if parsed.host_str() == Some("session") {
                "/chat"
            } else {
                "/apps/show"
            };
            format!("{prefix}/{identifier}")
        }
        ("settings", "") => "/settings/general".to_owned(),
        ("settings", "/computer-use") => "/settings/computer-use".to_owned(),
        ("settings", "/shortcuts") => "/settings/shortcuts".to_owned(),
        ("vaults", path) => {
            let identifier = path.strip_prefix("/request/")?;
            if !valid_identifier(identifier) {
                return None;
            }
            format!("/vaults?request_id={identifier}")
        }
        _ => return None,
    };
    Some(DeepLinkTarget { path })
}

fn valid_identifier(identifier: &str) -> bool {
    !identifier.is_empty()
        && identifier.len() <= 128
        && !matches!(identifier, "." | "..")
        && identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

#[derive(Clone)]
pub struct DeepLinkNavigation {
    destination: Url,
    delivery: Option<Arc<DeepLinkTarget>>,
}

impl DeepLinkNavigation {
    pub fn url(&self) -> &Url {
        &self.destination
    }

    pub fn show_session(&self) -> Option<&str> {
        self.delivery.as_ref()?.path.strip_prefix("/apps/show/")
    }
}

#[derive(Clone)]
pub struct DeepLinkReturn {
    delivery: Arc<DeepLinkTarget>,
    pub expected_generation: u64,
    pub action_owner: u64,
}

struct LocalReturn {
    ticket: DeepLinkReturn,
    in_flight: bool,
    succeeded: bool,
}

#[derive(Default)]
pub struct DeepLinks {
    pending: Option<Arc<DeepLinkTarget>>,
    action_owner: Option<u64>,
    local_return: Option<LocalReturn>,
    failed: bool,
    app_launch: Option<Arc<DeepLinkTarget>>,
}

impl DeepLinks {
    pub fn has_pending(&self) -> bool {
        self.pending.is_some()
    }

    /// Supersede navigation without resetting bootstrap failure or a native
    /// local-return operation that still needs to settle its original ticket.
    pub fn clear_pending(&mut self) {
        self.pending = None;
    }

    pub fn receive(&mut self, arguments: impl IntoIterator<Item = impl AsRef<str>>) {
        self.receive_owned(arguments, None);
    }

    /// Native input is authorized by the explicit action it interrupted.
    /// Lifecycle callbacks never refresh this authority.
    pub fn receive_owned(&mut self, arguments: impl IntoIterator<Item = impl AsRef<str>>, owner: Option<u64>) {
        if self.failed {
            return;
        }
        if let Some(target) = arguments
            .into_iter()
            .find_map(|argument| parse_deep_link(argument.as_ref()))
        {
            self.pending = Some(Arc::new(target));
            self.action_owner = owner;
        }
    }

    pub fn pending_for_owner(&self, owner: Option<u64>) -> bool {
        self.pending.as_ref().is_some_and(|pending| {
            (self.action_owner.is_none() || self.action_owner == owner)
                && !self.local_return.as_ref().is_some_and(|attempt| {
                    !attempt.in_flight && !attempt.succeeded && Arc::ptr_eq(&attempt.ticket.delivery, pending)
                })
        })
    }

    /// One attempt per received link, and at most one native return in flight.
    /// A failed/stale task retains the link without minting another action.
    pub fn claim_local_return(&mut self, generation: u64, owner: u64) -> Option<DeepLinkReturn> {
        if !self.pending_for_owner(Some(owner)) {
            return None;
        }
        let delivery = self.pending.as_ref()?;
        if self
            .local_return
            .as_ref()
            .is_some_and(|previous| previous.in_flight || Arc::ptr_eq(&previous.ticket.delivery, delivery))
        {
            return None;
        }
        let ticket = DeepLinkReturn {
            delivery: delivery.clone(),
            expected_generation: generation,
            action_owner: owner,
        };
        self.local_return = Some(LocalReturn {
            ticket: ticket.clone(),
            in_flight: true,
            succeeded: false,
        });
        Some(ticket)
    }

    /// A presentation approval belongs to one delivery, even when its successor
    /// has the same URL. Lifecycle may advance separately from this action owner.
    pub fn owns_local_return(&self, ticket: &DeepLinkReturn) -> bool {
        self.pending
            .as_ref()
            .is_some_and(|pending| Arc::ptr_eq(pending, &ticket.delivery))
            && self.action_owner.is_none_or(|owner| owner == ticket.action_owner)
            && self.local_return.as_ref().is_some_and(|current| {
                current.in_flight
                    && Arc::ptr_eq(&current.ticket.delivery, &ticket.delivery)
                    && current.ticket.expected_generation == ticket.expected_generation
                    && current.ticket.action_owner == ticket.action_owner
            })
    }

    pub fn finish_local_return(&mut self, ticket: &DeepLinkReturn, accepted_owner: Option<u64>) {
        let Some(current) = self
            .local_return
            .as_mut()
            .filter(|current| Arc::ptr_eq(&current.ticket.delivery, &ticket.delivery))
        else {
            return;
        };
        current.in_flight = false;
        current.succeeded = accepted_owner.is_some();
        if accepted_owner.is_some() && self.action_owner == Some(ticket.action_owner) {
            // A newer link received during this same accepted transition can
            // share its local presentation, but never a newer explicit action.
            // Lifecycle may have advanced before acceptance; use its receipt.
            self.action_owner = accepted_owner;
        }
    }

    pub fn observe_bootstrap(&mut self, status: &BootstrapStatus) {
        self.failed = status.phase == BootstrapPhase::Failed
            && status.notice.code != BootstrapNoticeCode::WorkbenchNavigationFailed;
        if self.failed {
            self.pending = None;
        }
    }

    pub fn bootstrap_navigation(&mut self, status: &BootstrapStatus) -> Option<DeepLinkNavigation> {
        self.bootstrap_navigation_owned(status, None)
    }

    pub fn bootstrap_navigation_owned(
        &mut self,
        status: &BootstrapStatus,
        owner: Option<u64>,
    ) -> Option<DeepLinkNavigation> {
        self.observe_bootstrap(status);
        if status.phase != BootstrapPhase::Ready {
            return None;
        }
        let origin = LoopbackOrigin::parse(&status.origin).ok()?;
        Some(
            if self.pending_for_owner(owner)
                && !self
                    .pending
                    .as_ref()
                    .is_some_and(|target| target.path.starts_with("/apps/show/"))
            {
                self.prepare_navigation(&origin)
            } else {
                DeepLinkNavigation {
                    destination: origin.navigation_url(),
                    delivery: None,
                }
            },
        )
    }

    pub fn workbench_navigation(&self, origin: &LoopbackOrigin, current_url: &Url) -> Option<DeepLinkNavigation> {
        if !origin.matches_url_origin(current_url) {
            return None;
        }
        self.pending.as_ref()?;
        Some(self.prepare_navigation(origin))
    }

    /// Claim exactly one Show delivery without consuming it as main navigation.
    /// A later same-URL delivery has a distinct Arc and remains independently live.
    pub fn claim_app_launch(&mut self, navigation: &DeepLinkNavigation) -> bool {
        let Some(delivery) = &navigation.delivery else {
            return false;
        };
        if navigation.show_session().is_none()
            || !self
                .pending
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, delivery))
            || self
                .app_launch
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, delivery))
        {
            return false;
        }
        self.app_launch = Some(delivery.clone());
        true
    }

    pub fn owns_app_launch(&self, navigation: &DeepLinkNavigation) -> bool {
        navigation.delivery.as_ref().is_some_and(|delivery| {
            self.pending
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, delivery))
                && self
                    .app_launch
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, delivery))
        })
    }

    /// Gives a claimed Show delivery back without consuming it: its launch went
    /// stale against a document that is being replaced, or never started. The
    /// delivery stays pending, so the replacement document can claim it again.
    pub fn release_app_launch(&mut self, navigation: &DeepLinkNavigation) -> bool {
        let owned = self.owns_app_launch(navigation);
        if owned {
            self.app_launch = None;
        }
        owned
    }

    pub fn commit_navigation(
        &mut self,
        navigation: &DeepLinkNavigation,
        navigation_succeeded: bool,
        issuing_generation: u64,
        current_generation: u64,
    ) -> bool {
        if !navigation_succeeded || issuing_generation != current_generation {
            return false;
        }
        match (&self.pending, &navigation.delivery) {
            (Some(pending), Some(delivery)) if Arc::ptr_eq(pending, delivery) => {
                self.pending = None;
                self.app_launch = None;
                true
            }
            _ => false,
        }
    }

    fn prepare_navigation(&self, origin: &LoopbackOrigin) -> DeepLinkNavigation {
        DeepLinkNavigation {
            destination: self
                .pending
                .as_ref()
                .map_or_else(|| origin.navigation_url(), |target| target.navigation_url(origin)),
            delivery: self.pending.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn superseding_navigation_preserves_failure_and_inflight_return() {
        let mut links = DeepLinks::default();
        links.receive_owned(["avibe://session/first"], Some(7));
        let ticket = links.claim_local_return(1, 7).unwrap();
        links.clear_pending();
        assert!(!links.owns_local_return(&ticket));
        links.receive_owned(["avibe://session/second"], Some(7));
        assert!(links.claim_local_return(2, 7).is_none());
        links.finish_local_return(&ticket, Some(8));
        assert!(links.pending_for_owner(Some(8)));
        assert!(links.claim_local_return(2, 8).is_some());
        links.failed = true;
        links.clear_pending();
        links.receive(["avibe://session/third"]);
        assert!(!links.has_pending());
    }

    #[test]
    fn local_return_approval_is_bound_to_exact_delivery_and_ticket() {
        let mut links = DeepLinks::default();
        links.receive_owned(["avibe://settings"], Some(4));
        let first = links.claim_local_return(12, 4).unwrap();
        assert!(links.owns_local_return(&first));
        for (generation, owner) in [(13, 4), (12, 5)] {
            let mut forged = first.clone();
            forged.expected_generation = generation;
            forged.action_owner = owner;
            assert!(!links.owns_local_return(&forged));
        }
        // The same URL delivered again is another action, not approval of the old one.
        links.receive_owned(["avibe://settings"], Some(4));
        assert!(!links.owns_local_return(&first));
        links.finish_local_return(&first, None);
        let second = links.claim_local_return(13, 4).unwrap();
        assert!(links.owns_local_return(&second));
        links.finish_local_return(&first, None);
        assert!(links.owns_local_return(&second));
        links.finish_local_return(&second, None);
        assert!(!links.owns_local_return(&second));
        assert!(links.claim_local_return(13, 4).is_none());
        links.receive_owned(["avibe://settings"], Some(5));
        assert!(links.claim_local_return(14, 5).is_some());
    }

    #[test]
    fn show_bootstrap_keeps_the_root_and_claims_one_exact_app_delivery() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let ready = BootstrapStatus::ready(&origin, 1, BootstrapNoticeCode::Ready);
        let mut links = DeepLinks::default();
        links.receive_owned(["avibe://show/session"], Some(4));
        let cold = links.bootstrap_navigation_owned(&ready, Some(4)).unwrap();
        assert_eq!(cold.url(), &origin.navigation_url());
        assert!(!links.commit_navigation(&cold, true, 1, 1));
        let first = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
        assert_eq!(first.show_session(), Some("session"));
        assert!(links.claim_app_launch(&first));
        assert!(!links.claim_app_launch(&first));
        links.receive_owned(["avibe://show/session"], Some(4));
        assert!(!links.owns_app_launch(&first));
        let next = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
        assert!(links.claim_app_launch(&next));
        assert!(!links.commit_navigation(&first, true, 1, 1));
        assert!(!links.commit_navigation(&next, true, 1, 2));
        assert!(links.commit_navigation(&next, true, 2, 2));
        assert!(!links.has_pending());
    }

    #[test]
    fn a_stale_show_launch_keeps_its_delivery_for_the_replacement_document() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let mut links = DeepLinks::default();
        links.receive_owned(["avibe://show/session"], Some(4));
        let first = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
        assert!(links.claim_app_launch(&first));
        assert!(links.release_app_launch(&first));
        assert!(links.has_pending(), "a stale launch must not consume the delivery");
        assert!(!links.owns_app_launch(&first));
        assert!(!links.release_app_launch(&first), "only the owning claim releases");

        let retry = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
        assert_eq!(retry.show_session(), Some("session"));
        assert!(links.claim_app_launch(&retry));
        assert!(links.commit_navigation(&retry, true, 1, 1));
        assert!(!links.has_pending());
        assert!(
            !links.release_app_launch(&retry),
            "a committed delivery has nothing to release"
        );
    }

    const MAPPINGS: &[(&str, &str)] = &[
        ("avibe://session/ses.A_b-9", "/chat/ses.A_b-9"),
        ("avibe://show/ses.A_b-9", "/apps/show/ses.A_b-9"),
        ("avibe://settings", "/settings/general"),
        ("avibe://settings/computer-use", "/settings/computer-use"),
        ("avibe://settings/shortcuts", "/settings/shortcuts"),
        ("avibe://vaults/request/req.A_b-9", "/vaults?request_id=req.A_b-9"),
    ];

    #[test]
    fn every_contract_mapping_preserves_its_path_and_the_adopted_origin() {
        for origin in ["http://127.0.0.1:35123", "http://[::1]:49152"] {
            let origin = LoopbackOrigin::parse(origin).unwrap();
            for (input, path) in MAPPINGS {
                let target = parse_deep_link(input).unwrap();
                assert_eq!(target.path(), *path);
                let destination = target.navigation_url(&origin);
                assert_eq!(destination.as_str(), format!("{}{path}", origin.as_str()));
                assert!(origin.matches_url_origin(&destination));
                assert!(!crate::is_shell_ui_url(&destination));
            }
        }
    }

    #[test]
    fn identifiers_are_bounded_literal_path_segments_not_url_syntax() {
        for prefix in ["avibe://session/", "avibe://show/", "avibe://vaults/request/"] {
            for length in 1..=128 {
                let identifier = "a".repeat(length);
                assert!(parse_deep_link(&format!("{prefix}{identifier}")).is_some());
            }
            assert!(parse_deep_link(&format!("{prefix}{}", "a".repeat(129))).is_none());
            for byte in 0..=127_u8 {
                let identifier = format!("a{}b", char::from(byte));
                let allowed = byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-');
                assert_eq!(parse_deep_link(&format!("{prefix}{identifier}")).is_some(), allowed);
            }
            for identifier in ["", ".", "..", "%2E", "%2E%2E", "你好", "a/b", "a\\b", "a?x", "a#x"] {
                assert!(parse_deep_link(&format!("{prefix}{identifier}")).is_none());
            }
        }
    }

    #[test]
    fn only_the_literal_frozen_grammar_can_produce_a_navigation() {
        for (input, _) in MAPPINGS {
            for malformed in [
                input.replacen("avibe:", "https:", 1),
                input.replacen("avibe://", "avibe://host@", 1),
                input.replacen("avibe://", "avibe:///", 1),
                input.replacen("avibe://", "avibe://unknown/", 1),
                input.replacen("avibe://", "avibe:", 1),
                format!("{input}/"),
                format!("{input}/extra"),
                format!("{input}?unexpected"),
                format!("{input}#fragment"),
                format!(" {input}"),
                format!("{input}\n"),
            ] {
                assert!(parse_deep_link(&malformed).is_none(), "{malformed:?}");
            }
        }
        for malformed in [
            "avibe://session:80/id",
            "avibe://session@show/id",
            "avibe://@session/id",
            "avibe://session/id/../normalized",
            "avibe://session/id/%2E%2E/normalized",
            "avibe://session/id/./normalized",
        ] {
            assert!(parse_deep_link(malformed).is_none(), "{malformed:?}");
        }
    }

    #[test]
    fn hot_delivery_waits_for_the_adopted_workbench_and_applies_once() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let mut links = DeepLinks::default();
        links.receive(["avibe", "--flag", "avibe://session/hot"]);
        for current in ["tauri://localhost/", "http://localhost:1420/", "http://127.0.0.1:5123/"] {
            assert!(links
                .workbench_navigation(&origin, &Url::parse(current).unwrap())
                .is_none());
        }
        let navigation = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
        assert_eq!(navigation.url().as_str(), "http://127.0.0.1:39567/chat/hot");
        assert!(links.commit_navigation(&navigation, true, 1, 1));
        assert!(links.workbench_navigation(&origin, &origin.navigation_url()).is_none());
    }

    #[test]
    fn a_delivery_selects_one_valid_link_and_invalid_input_cannot_erase_it() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let mut links = DeepLinks::default();
        links.receive(["avibe", "avibe://session/first", "avibe://session/second"]);
        links.receive(["avibe://session/../settings"]);
        assert_eq!(
            links
                .workbench_navigation(&origin, &origin.navigation_url())
                .unwrap()
                .url()
                .path(),
            "/chat/first"
        );
    }

    #[test]
    fn only_success_in_the_issuing_window_consumes_a_prepared_delivery() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let issuing_generation = 2;
        for navigation_succeeded in [false, true] {
            for current_generation in 1..=3 {
                let mut links = DeepLinks::default();
                links.receive(["avibe://session/pending"]);
                let navigation = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
                assert!(links.workbench_navigation(&origin, &origin.navigation_url()).is_some());
                let should_commit = navigation_succeeded && issuing_generation == current_generation;
                assert_eq!(
                    links.commit_navigation(
                        &navigation,
                        navigation_succeeded,
                        issuing_generation,
                        current_generation
                    ),
                    should_commit
                );
                assert_eq!(
                    links.workbench_navigation(&origin, &origin.navigation_url()).is_none(),
                    should_commit
                );
                assert!(!links.commit_navigation(&navigation, false, issuing_generation, current_generation));
            }
        }
    }

    #[test]
    fn an_older_commit_never_clears_a_newer_delivery_even_for_the_same_url() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let ready = BootstrapStatus::ready(&origin, 1, BootstrapNoticeCode::Ready);
        for first in [None, Some("avibe://session/first")] {
            for next in ["avibe://session/first", "avibe://show/newer"] {
                let mut links = DeepLinks::default();
                links.receive(first);
                let older = links.bootstrap_navigation(&ready).unwrap();
                links.receive([next]);
                assert!(!links.commit_navigation(&older, true, 1, 1));
                let newer = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
                assert_eq!(newer.url(), &parse_deep_link(next).unwrap().navigation_url(&origin));
                assert!(links.commit_navigation(&newer, true, 1, 1));
                assert!(!links.commit_navigation(&newer, true, 1, 1));
                assert!(links.workbench_navigation(&origin, &origin.navigation_url()).is_none());
            }
        }
    }
}
