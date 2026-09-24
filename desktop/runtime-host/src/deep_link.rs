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
        ("settings", "") => "/admin/settings/service".to_owned(),
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

pub struct DeepLinkNavigation {
    destination: Url,
    delivery: Option<Arc<DeepLinkTarget>>,
}

impl DeepLinkNavigation {
    pub fn url(&self) -> &Url {
        &self.destination
    }
}

#[derive(Default)]
pub struct DeepLinks {
    pending: Option<Arc<DeepLinkTarget>>,
    failed: bool,
}

impl DeepLinks {
    pub fn receive(&mut self, arguments: impl IntoIterator<Item = impl AsRef<str>>) {
        if self.failed {
            return;
        }
        if let Some(target) = arguments
            .into_iter()
            .find_map(|argument| parse_deep_link(argument.as_ref()))
        {
            self.pending = Some(Arc::new(target));
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
        self.observe_bootstrap(status);
        if status.phase != BootstrapPhase::Ready {
            return None;
        }
        let origin = LoopbackOrigin::parse(&status.origin).ok()?;
        Some(self.prepare_navigation(&origin))
    }

    pub fn workbench_navigation(&self, origin: &LoopbackOrigin, current_url: &Url) -> Option<DeepLinkNavigation> {
        if !origin.matches_url_origin(current_url) {
            return None;
        }
        self.pending.as_ref()?;
        Some(self.prepare_navigation(origin))
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

    const MAPPINGS: &[(&str, &str)] = &[
        ("avibe://session/ses.A_b-9", "/chat/ses.A_b-9"),
        ("avibe://show/ses.A_b-9", "/apps/show/ses.A_b-9"),
        ("avibe://settings", "/admin/settings/service"),
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
