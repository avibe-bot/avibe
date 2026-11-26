use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use super::ResultPage;

/// Plain text from desktopBootstrap.providerCallback, in the asking App's language.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CallbackCopy {
    pub done: String,
    pub failed: String,
    pub verify_in_app: String,
    pub cancelled: String,
    pub timed_out: String,
    pub account_conflict: String,
    pub server_error: String,
    pub denied: String,
    pub email_unverified: String,
    pub retry: String,
    pub account_conflict_next: String,
    pub details: String,
    pub open_app: String,
}

// The same script is hash-authorized on both pages. Fragment data can only become
// this listener's callback query; native still owns grammar and state validation.
// Final pages scrub the URL so copying it does not copy authentication material.
const SCRIPT: &str = r#"(() => {
  if (document.body.dataset.fragment === "true") {
    const query = new URLSearchParams(location.search);
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (!fragment.has("error") && !fragment.has("code")) return;
    const keys = new Set(["state", "code", "error", "error_code", "error_description"]);
    for (const [key, value] of fragment) {
      if (!keys.has(key) || fragment.getAll(key).length !== 1) return;
      if (query.has(key) && query.get(key) !== value) return;
      query.set(key, value);
    }
    location.replace(location.pathname + "?" + query.toString());
  } else {
    history.replaceState(null, "", location.pathname);
  }
})();"#;

pub(super) fn script_hash() -> String {
    STANDARD.encode(Sha256::digest(SCRIPT.as_bytes()))
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

pub(super) fn result(copy: &CallbackCopy, result: &ResultPage) -> String {
    let (title, explanation, detail) = match result {
        ResultPage::Complete => (&copy.done, "", None),
        ResultPage::VerifyInApp => (&copy.verify_in_app, "", None),
        ResultPage::Cancelled => (&copy.cancelled, copy.retry.as_str(), None),
        ResultPage::TimedOut => (&copy.timed_out, copy.retry.as_str(), None),
        ResultPage::Failed(failure) => {
            let reason = match failure.code {
                "provider_account_conflict" => &copy.account_conflict,
                "provider_server_error" => &copy.server_error,
                "provider_denied" => &copy.denied,
                "provider_email_unverified" => &copy.email_unverified,
                _ => &copy.failed,
            };
            let next = if failure.code == "provider_account_conflict" {
                &copy.account_conflict_next
            } else {
                &copy.retry
            };
            (reason, next.as_str(), failure.description.as_deref())
        }
    };
    render(copy, title, explanation, detail, false)
}

pub(super) fn fragment(copy: &CallbackCopy) -> String {
    render(copy, &copy.verify_in_app, &copy.retry, None, true)
}

fn render(copy: &CallbackCopy, title: &str, explanation: &str, detail: Option<&str>, fragment: bool) -> String {
    let detail = detail.map_or(String::new(), |detail| {
        format!(
            "<details><summary>{}</summary><p>{}</p></details>",
            escape(&copy.details),
            escape(detail)
        )
    });
    format!(
        r#"<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Avibe</title><style>
body{{font:16px/1.6 system-ui,sans-serif;margin:0;background:#f8fafc;color:#17212b}}
main{{max-width:36rem;margin:12vh auto;padding:2rem}}h1{{font-size:1.6rem;line-height:1.3}}
a{{display:inline-block;padding:.65rem 1.2rem;background:#146c53;color:white;border-radius:.6rem;text-decoration:none}}
details{{margin:1.5rem 0;color:#52606d;font-size:.85rem;overflow-wrap:anywhere}}details p{{white-space:pre-wrap}}
@media(prefers-color-scheme:dark){{body{{background:#13171c;color:#eef2f6}}details{{color:#bac5d0}}}}
</style></head><body data-fragment="{fragment}"><main><p>Avibe</p><h1>{}</h1><p>{}</p>{detail}<a href="avibe://open">{}</a></main><script>{SCRIPT}</script></body></html>"#,
        escape(title),
        escape(explanation),
        escape(&copy.open_app),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::instance_client::loopback::ProviderFailure;

    #[test]
    fn account_conflict_offers_another_method_without_suggesting_a_retry() {
        let copy = CallbackCopy {
            retry: "Retry or choose another method".into(),
            account_conflict_next: "Use another method".into(),
            ..Default::default()
        };
        let conflict = result(
            &copy,
            &ResultPage::Failed(ProviderFailure::from_code("provider_account_conflict")),
        );
        assert!(conflict.contains(&copy.account_conflict_next));
        assert!(!conflict.contains(&copy.retry));
        let server = result(
            &copy,
            &ResultPage::Failed(ProviderFailure::from_code("provider_server_error")),
        );
        assert!(server.contains(&copy.retry));
    }
}
