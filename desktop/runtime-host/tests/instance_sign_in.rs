//! AUTH-SETUP-183/184: Desktop provider sign-in (loopback PKCE) and passkey
//! sign-in with no email, through the coordinator, over a fake avibe.bot and
//! Supabase. The fake browser is a synthetic consumer of the loopback
//! listener; no real browser, provider, account or passkey is involved.
use async_trait::async_trait;
use avibe_runtime_host::instance_client::{
    account::{Passkeys, SystemBrowser},
    coordinator::{ClientView, Coordinator},
    protocol::*,
    storage::{ConnectionStore, Credentials},
    transport::{Epoch, NativeSession, Request, Response, Transport},
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use reqwest::header::{HeaderMap, SET_COOKIE};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::Notify,
};
use url::Url;

/// One request as avibe.bot or Supabase received it.
#[derive(Clone)]
struct Seen {
    path: String,
    query: Option<String>,
    headers: HeaderMap,
    body: Option<Value>,
}
/// The request held (1-based), told when it arrives, and its release.
type Held = (usize, Arc<Notify>, Arc<Notify>);
struct Http {
    replies: Mutex<VecDeque<(&'static str, Response)>>,
    seen: Mutex<Vec<Seen>>,
    /// A request whose reply is held: it takes its reply in order, says so,
    /// then answers once released.
    held: Mutex<Option<Held>>,
}
#[async_trait]
impl Transport for Http {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let path = request.url.path().to_owned();
        self.seen.lock().unwrap().push(Seen {
            path: path.clone(),
            query: request.url.query().map(str::to_owned),
            headers: request.headers.clone(),
            body: request.body.clone(),
        });
        let (expected, reply) = self
            .replies
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected HTTP request");
        assert_eq!(path, expected);
        let count = self.seen.lock().unwrap().len();
        let hold = {
            let mut held = self.held.lock().unwrap();
            match held.as_ref() {
                Some((at, _, _)) if *at == count => held.take().map(|(_, entered, release)| (entered, release)),
                _ => None,
            }
        };
        if let Some((entered, release)) = hold {
            entered.notify_one();
            release.notified().await;
        }
        Ok(reply)
    }
}
fn reply(status: u16, body: Value, cookie: Option<&str>) -> Response {
    let mut headers = HeaderMap::new();
    if let Some(cookie) = cookie {
        headers.insert(SET_COOKIE, cookie.parse().unwrap());
    }
    Response {
        status,
        body: serde_json::to_vec(&body).unwrap(),
        headers,
    }
}

#[derive(Default)]
struct Private(Mutex<HashMap<String, Vec<u8>>>);
impl Credentials for Private {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        Ok(self.0.lock().unwrap().get(id).cloned())
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        self.0.lock().unwrap().insert(id.into(), bytes.into());
        Ok(())
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        self.0.lock().unwrap().remove(id);
        Ok(())
    }
}

#[derive(Default)]
struct View;
#[async_trait]
impl ClientView for View {
    fn changed(&self, _: &ClientState) {}
    fn invalidate(&self, _: u64) {}
    async fn commit(&self, epoch: Epoch, _: ClientTarget, _: Vec<cookie::Cookie<'static>>) -> Result<(), ClientError> {
        epoch.check()
    }
    async fn restore(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<(), ClientError> {
        Ok(())
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<(), ClientError> {
        Ok(())
    }
}

/// What the synthetic browser does once the app opens the authorization page.
#[derive(Clone)]
enum Browsing {
    /// Supabase redirects back with this code.
    Code(&'static str),
    /// Supabase redirects back with an error query.
    Error(&'static str),
    /// A stray page hits the listener first, then the real redirect arrives.
    StrayThenCode(&'static str),
    /// The person never finishes in the browser.
    Nothing,
}
struct Browser {
    browsing: Mutex<VecDeque<Browsing>>,
    opened: Mutex<Vec<Url>>,
    redirected: Arc<Notify>,
}
impl SystemBrowser for Browser {
    fn open(&self, url: &Url) -> Result<(), ClientError> {
        self.opened.lock().unwrap().push(url.clone());
        let redirect = url
            .query_pairs()
            .find(|(key, _)| key == "redirect_to")
            .map(|(_, value)| Url::parse(&value).unwrap())
            .unwrap();
        let browsing = {
            let mut queue = self.browsing.lock().unwrap();
            if queue.len() > 1 {
                queue.pop_front().unwrap()
            } else {
                queue.front().cloned().unwrap()
            }
        };
        let redirected = self.redirected.clone();
        tokio::spawn(async move {
            let port = redirect.port().unwrap();
            let target = |extra: &str| format!("{}?{}&{extra}", redirect.path(), redirect.query().unwrap());
            match browsing {
                Browsing::Code(code) => {
                    get(port, &target(&format!("code={code}"))).await;
                }
                Browsing::Error(query) => {
                    get(port, &target(query)).await;
                }
                Browsing::StrayThenCode(code) => {
                    get(port, "/callback?state=forged&code=stolen").await;
                    get(port, &target(&format!("code={code}"))).await;
                }
                Browsing::Nothing => {}
            }
            redirected.notify_one();
        });
        Ok(())
    }
}
async fn get(port: u16, target: &str) -> String {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)).await else {
        return String::new();
    };
    let _ = stream
        .write_all(format!("GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes())
        .await;
    let mut body = String::new();
    let _ = stream.read_to_string(&mut body).await;
    body
}

struct FakePasskeys {
    result: Result<Value, &'static str>,
    options: Mutex<Vec<Value>>,
    entered: Arc<Notify>,
    hold: Option<Arc<Notify>>,
}
/// An OS without a passkey sheet.
struct NoPasskeySheet;
#[async_trait]
impl Passkeys for NoPasskeySheet {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value, ClientError> {
        Err(ClientError::new("passkey_unavailable"))
    }
    fn supported(&self) -> bool {
        false
    }
}
#[async_trait]
impl Passkeys for FakePasskeys {
    async fn assert(&self, options: Value, epoch: Epoch) -> Result<Value, ClientError> {
        self.options.lock().unwrap().push(options);
        self.entered.notify_one();
        if let Some(hold) = &self.hold {
            hold.notified().await;
        }
        epoch.check()?;
        self.result.clone().map_err(ClientError::new)
    }
}

struct Fixture {
    client: Arc<Coordinator>,
    http: Arc<Http>,
    private: Arc<Private>,
    _root: tempfile::TempDir,
}
fn fixture(
    replies: Vec<(&'static str, Response)>,
    browser: Option<Arc<Browser>>,
    passkeys: Arc<FakePasskeys>,
) -> Fixture {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("Studio").unwrap();
    let state = ClientState::new("Studio".into(), "0.1.0".into(), json!({}));
    let http = Arc::new(Http {
        replies: Mutex::new(replies.into()),
        seen: Mutex::new(vec![]),
        held: Mutex::new(None),
    });
    let private = Arc::new(Private::default());
    let mut client = Coordinator::new(
        state,
        record,
        store,
        http.clone(),
        private.clone(),
        passkeys,
        Arc::new(View),
    );
    if let Some(browser) = browser {
        client = client.with_browser(browser);
    }
    Fixture {
        client: Arc::new(client),
        http,
        private,
        _root: root,
    }
}
fn passkeys(result: Result<Value, &'static str>) -> Arc<FakePasskeys> {
    Arc::new(FakePasskeys {
        result,
        options: Mutex::new(vec![]),
        entered: Arc::new(Notify::new()),
        hold: None,
    })
}
fn browser(browsing: Browsing) -> Arc<Browser> {
    browsers(vec![browsing])
}
fn browsers(browsing: Vec<Browsing>) -> Arc<Browser> {
    Arc::new(Browser {
        browsing: Mutex::new(browsing.into()),
        opened: Mutex::new(vec![]),
        redirected: Arc::new(Notify::new()),
    })
}
/// Tests that check that a port was released run one at a time, so no other
/// test's listener is on the port they check. Why one such check once failed
/// in a parallel run is not known; that run's log was not kept, and later
/// passes under this lock are an observation, not a fix.
static PORTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn act(client: &Coordinator, action: ClientAction) -> ClientReply {
    client
        .request(ClientRequest {
            version: 1,
            request_id: "unit".into(),
            expected_generation: client.state().generation,
            action,
        })
        .await
}
fn config(google: bool) -> Response {
    reply(
        200,
        json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture",
            "google_login_enabled":google,"github_login_enabled":true,"apple_login_enabled":false}),
        None,
    )
}
fn tokens() -> Response {
    reply(
        200,
        json!({"access_token":"synthetic-access","refresh_token":"synthetic-refresh"}),
        None,
    )
}
fn two_factor() -> Response {
    reply(
        403,
        json!({"error":"two_factor_required","email":"user@example.test","gate":"challenge","methods":["totp"]}),
        Some("account=fixture; Path=/; Secure; HttpOnly"),
    )
}
fn signed_in() -> Value {
    serde_json::from_str::<Value>(include_str!("../../contracts/instance-client-v1/transport.json")).unwrap()
        ["account_session"]
        .clone()
}
fn google() -> ClientAction {
    serde_json::from_value(json!({"type":"sign_in_with_provider","provider":"google"})).unwrap()
}

/// AUTH-SETUP-183: the browser's code is redeemed with this attempt's own
/// verifier, then the email code's session endpoint and two-step gate apply.
#[tokio::test]
async fn provider_sign_in_redeems_its_own_code_and_keeps_the_two_step_gate() {
    let _ports = PORTS.lock().await;
    let browser = browser(Browsing::StrayThenCode("synthetic-code"));
    let f = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            ("/api/desktop/session", two_factor()),
        ],
        Some(browser.clone()),
        passkeys(Err("passkey_unavailable")),
    );
    let before = f.client.state().generation;
    let outcome = act(&f.client, google()).await;
    assert!(outcome.ok, "{:?}", outcome.error);
    assert_eq!(outcome.state.account.status, AccountStatus::SecondFactorRequired);
    assert_eq!(
        outcome.state.generation,
        before + 1,
        "a provider sign-in starts a new attempt"
    );

    let opened = browser.opened.lock().unwrap()[0].clone();
    assert_eq!(opened.origin().ascii_serialization(), "https://provider.test");
    assert_eq!(opened.path(), "/auth/v1/authorize");
    let query: HashMap<String, String> = opened.query_pairs().into_owned().collect();
    assert_eq!(query["provider"], "google");
    assert_eq!(query["code_challenge_method"], "s256");
    let redirect = Url::parse(&query["redirect_to"]).unwrap();
    assert_eq!(
        (redirect.scheme(), redirect.host_str(), redirect.path()),
        ("http", Some("127.0.0.1"), "/callback")
    );

    let seen = f.http.seen.lock().unwrap().clone();
    let token = seen.iter().find(|request| request.path == "/auth/v1/token").unwrap();
    assert_eq!(token.query.as_deref(), Some("grant_type=pkce"));
    let body = token.body.clone().unwrap();
    assert_eq!(
        body["auth_code"], "synthetic-code",
        "the forged callback was never redeemed"
    );
    let verifier = body["code_verifier"].as_str().unwrap();
    assert_eq!(
        URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes())),
        query["code_challenge"]
    );
    let session = seen
        .iter()
        .find(|request| request.path == "/api/desktop/session")
        .unwrap();
    assert_eq!(session.body.clone().unwrap()["access_token"], "synthetic-access");
    // The attempt's port is gone: a late redirect cannot reach a new attempt.
    assert!(TcpStream::connect(("127.0.0.1", redirect.port().unwrap()))
        .await
        .is_err());
}

#[tokio::test]
async fn provider_sign_in_signs_in_and_keeps_tokens_out_of_the_published_state() {
    let _ports = PORTS.lock().await;
    let f = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            (
                "/api/desktop/session",
                reply(200, signed_in(), Some("account=fixture; Path=/; Secure; HttpOnly")),
            ),
        ],
        Some(browser(Browsing::Code("synthetic-code"))),
        passkeys(Err("passkey_unavailable")),
    );
    let outcome = act(&f.client, google()).await;
    assert!(outcome.ok, "{:?}", outcome.error);
    assert_eq!(outcome.state.account.status, AccountStatus::SignedIn);
    let published = serde_json::to_string(&outcome).unwrap();
    for secret in ["synthetic-code", "synthetic-access", "synthetic-refresh"] {
        assert!(!published.contains(secret));
    }
    assert!(
        !f.private.0.lock().unwrap().is_empty(),
        "the cookie jar is kept natively"
    );
}

#[tokio::test]
async fn provider_sign_in_answers_each_ending_with_its_code() {
    let _ports = PORTS.lock().await;
    let cases: [(bool, Browsing, &str); 3] = [
        (false, Browsing::Nothing, "provider_unavailable"),
        (
            true,
            Browsing::Error("error=access_denied&error_code=provider_email_needs_verification&error_description=x"),
            "provider_email_unverified",
        ),
        (
            true,
            Browsing::Error("error=server_error&error_description=x"),
            "invalid_or_expired",
        ),
    ];
    for (enabled, browsing, code) in cases {
        let f = fixture(
            vec![("/api/config/auth", config(enabled))],
            Some(browser(browsing)),
            passkeys(Err("x")),
        );
        let outcome = act(&f.client, google()).await;
        assert_eq!(outcome.error.map(|error| error.code).as_deref(), Some(code));
        assert_eq!(outcome.state.account.status, AccountStatus::SignedOut);
        assert!(f
            .http
            .seen
            .lock()
            .unwrap()
            .iter()
            .all(|request| request.path != "/auth/v1/token"));
    }
    // A build with no browser offers nothing.
    let f = fixture(vec![("/api/config/auth", config(true))], None, passkeys(Err("x")));
    assert_eq!(
        act(&f.client, google()).await.error.unwrap().code,
        "provider_unavailable"
    );
}

/// The browser has no cancel the app can see: abort ends the wait, releases
/// the port and leaves the window where it is.
#[tokio::test]
async fn abort_ends_a_waiting_provider_sign_in_and_frees_its_port() {
    let _ports = PORTS.lock().await;
    let browser = browser(Browsing::Nothing);
    let f = fixture(
        vec![("/api/config/auth", config(true))],
        Some(browser.clone()),
        passkeys(Err("x")),
    );
    let client = f.client.clone();
    let pending = tokio::spawn(async move { act(&client, google()).await });
    browser.redirected.notified().await;
    let port = Url::parse(
        &browser.opened.lock().unwrap()[0]
            .query_pairs()
            .find(|(key, _)| key == "redirect_to")
            .unwrap()
            .1,
    )
    .unwrap()
    .port()
    .unwrap();
    // Abort ends the wait without waiting for it (a new attempt would too; see
    // a_new_attempt_retires_the_waiting_one_and_signs_in).
    assert!(act(&f.client, ClientAction::AbortSignIn).await.ok);
    let ended = tokio::time::timeout(Duration::from_secs(5), pending)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(ended.error.unwrap().code, "stale_generation");
    assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
    assert!(TcpStream::connect(("127.0.0.1", port)).await.is_err());
    // A signed-in account cannot be aborted out.
    let g = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            (
                "/api/desktop/session",
                reply(200, signed_in(), Some("account=fixture; Path=/; Secure; HttpOnly")),
            ),
        ],
        Some(self::browser(Browsing::Code("synthetic-code"))),
        passkeys(Err("x")),
    );
    assert!(act(&g.client, google()).await.ok);
    assert_eq!(
        act(&g.client, ClientAction::AbortSignIn).await.error.unwrap().code,
        "invalid_action"
    );
    assert_eq!(g.client.state().account.status, AccountStatus::SignedIn);
}

fn primary_options() -> Response {
    reply(
        200,
        json!({"ok":true,"flow":"flow-1","expiresAt":"2026-11-18T10:00:00Z",
            "options":{"challenge":"Y2hhbGxlbmdl","rpId":"avibe.bot","timeout":120000,"userVerification":"required",
                "allowCredentials":[{"type":"public-key","id":"c29tZQ"}]}}),
        Some("__Host-avibe_passkey_primary=binding; Path=/; Secure; HttpOnly; SameSite=Lax"),
    )
}

/// AUTH-SETUP-184: passkey sign-in sends no email, asks for a discoverable
/// credential, returns the binding cookie, and lands on session discovery.
#[tokio::test]
async fn passkey_sign_in_is_discoverable_and_cookie_bound() {
    let _ports = PORTS.lock().await;
    let keys = passkeys(Ok(
        json!({"id":"cred","rawId":"cred","type":"public-key","response":{"userHandle":"dXNlcg"}}),
    ));
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            (
                "/api/passkey-primary/verify",
                reply(
                    200,
                    json!({"ok":true,"next":"/"}),
                    Some("avibe_session=s; Path=/; Secure; HttpOnly"),
                ),
            ),
            ("/api/mobile/session", reply(200, signed_in(), None)),
        ],
        None,
        keys.clone(),
    );
    let outcome = act(&f.client, ClientAction::SignInWithPasskey).await;
    assert!(outcome.ok, "{:?}", outcome.error);
    assert_eq!(outcome.state.account.status, AccountStatus::SignedIn);
    let options = keys.options.lock().unwrap()[0].clone();
    assert!(options.get("allowCredentials").is_none(), "discoverable: no allow-list");
    let seen = f.http.seen.lock().unwrap().clone();
    let start = &seen[0];
    assert_eq!(start.headers.get("accept").unwrap(), "application/json");
    assert_eq!(start.headers.get("origin").unwrap(), "https://avibe.bot");
    assert!(start.headers.get("authorization").is_none());
    assert_eq!(
        start.body.clone().unwrap(),
        json!({"context":{"continuation":null,"next":null,"lang":null}})
    );
    let verify = &seen[1];
    assert!(verify
        .headers
        .get("cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .contains("__Host-avibe_passkey_primary=binding"));
    assert_eq!(verify.body.clone().unwrap()["flow"], "flow-1");
}

#[tokio::test]
async fn passkey_sign_in_maps_each_ending() {
    let _ports = PORTS.lock().await;
    // A closed sheet ends the flow on avibe.bot as well.
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/cancel", reply(200, json!({"ok":true}), None)),
        ],
        None,
        passkeys(Err("passkey_cancelled")),
    );
    let outcome = act(&f.client, ClientAction::SignInWithPasskey).await;
    assert_eq!(outcome.error.unwrap().code, "passkey_cancelled");
    assert_eq!(outcome.state.account.status, AccountStatus::SignedOut);
    for (status, error, code) in [
        (503, "unavailable", "provider_unavailable"),
        // 403 is the HTTP boundary refusing the request, not an audience answer.
        (403, "forbidden", "request_failed"),
        (429, "rate_limited", "rate_limited"),
    ] {
        let f = fixture(
            vec![(
                "/api/passkey-primary/options",
                reply(status, json!({"ok":false,"error":error}), None),
            )],
            None,
            passkeys(Err("x")),
        );
        assert_eq!(
            act(&f.client, ClientAction::SignInWithPasskey)
                .await
                .error
                .unwrap()
                .code,
            code
        );
    }
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            (
                "/api/passkey-primary/verify",
                reply(400, json!({"ok":false,"error":"verification_failed"}), None),
            ),
        ],
        None,
        passkeys(Ok(json!({"id":"cred"}))),
    );
    assert_eq!(
        act(&f.client, ClientAction::SignInWithPasskey)
            .await
            .error
            .unwrap()
            .code,
        "invalid"
    );
}

fn primary_verified() -> Response {
    reply(
        200,
        json!({"ok":true,"next":"/"}),
        Some("avibe_session=s; Path=/; Secure; HttpOnly"),
    )
}

/// AUTH-SETUP-184: avibe.bot accepted the passkey but reading its session
/// failed. The next attempt reads that session again from the same jar, with
/// no second sheet and no second verify (backend passkey-sign-in §6).
#[tokio::test]
async fn an_accepted_passkey_whose_session_read_failed_is_read_again() {
    let keys = passkeys(Ok(json!({"id":"cred","rawId":"cred","type":"public-key"})));
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/verify", primary_verified()),
            ("/api/mobile/session", reply(503, json!({"error":"unavailable"}), None)),
            ("/api/mobile/session", reply(200, signed_in(), None)),
        ],
        None,
        keys.clone(),
    );
    let failed = act(&f.client, ClientAction::SignInWithPasskey).await;
    assert!(!failed.ok);
    assert_eq!(failed.state.account.status, AccountStatus::SignedOut);
    let again = act(&f.client, ClientAction::SignInWithPasskey).await;
    assert!(again.ok, "{:?}", again.error);
    assert_eq!(again.state.account.status, AccountStatus::SignedIn);
    assert_eq!(keys.options.lock().unwrap().len(), 1, "one system sheet");
    let seen = f.http.seen.lock().unwrap().clone();
    let paths: Vec<_> = seen.iter().map(|request| request.path.as_str()).collect();
    assert_eq!(
        paths,
        [
            "/api/passkey-primary/options",
            "/api/passkey-primary/verify",
            "/api/mobile/session",
            "/api/mobile/session"
        ]
    );
    assert!(seen[3]
        .headers
        .get("cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .contains("avibe_session=s"));
}

/// A `401` on that read answers it: the attempt after runs the whole passkey
/// sign-in again. So does any other first factor in between.
#[tokio::test]
async fn an_answered_read_or_another_first_factor_ends_an_accepted_passkey() {
    let keys = passkeys(Ok(json!({"id":"cred","rawId":"cred","type":"public-key"})));
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/verify", primary_verified()),
            ("/api/mobile/session", reply(503, json!({"error":"unavailable"}), None)),
            ("/api/mobile/session", reply(401, json!({"error":"signed_out"}), None)),
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/verify", primary_verified()),
            ("/api/mobile/session", reply(200, signed_in(), None)),
        ],
        None,
        keys.clone(),
    );
    assert!(!act(&f.client, ClientAction::SignInWithPasskey).await.ok);
    assert_eq!(
        act(&f.client, ClientAction::SignInWithPasskey)
            .await
            .error
            .unwrap()
            .code,
        "signed_out"
    );
    let outcome = act(&f.client, ClientAction::SignInWithPasskey).await;
    assert!(outcome.ok, "{:?}", outcome.error);
    assert_eq!(keys.options.lock().unwrap().len(), 2);

    let keys = passkeys(Ok(json!({"id":"cred","rawId":"cred","type":"public-key"})));
    let f = fixture(
        vec![
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/verify", primary_verified()),
            ("/api/mobile/session", reply(503, json!({"error":"unavailable"}), None)),
            ("/api/config/auth", config(true)),
            (
                "/auth/v1/token",
                reply(400, json!({"error_code":"invalid_credentials"}), None),
            ),
            ("/api/passkey-primary/options", primary_options()),
            ("/api/passkey-primary/verify", primary_verified()),
            ("/api/mobile/session", reply(200, signed_in(), None)),
        ],
        None,
        keys.clone(),
    );
    assert!(!act(&f.client, ClientAction::SignInWithPasskey).await.ok);
    let password: ClientAction =
        serde_json::from_value(json!({"type":"sign_in_password","email":"max@example.test","password":"nope"}))
            .unwrap();
    assert_eq!(
        act(&f.client, password).await.error.unwrap().code,
        "invalid_credentials"
    );
    assert!(act(&f.client, ClientAction::SignInWithPasskey).await.ok);
    assert_eq!(keys.options.lock().unwrap().len(), 2);
}

/// An account change while the system sheet is up drops its late assertion.
#[tokio::test]
async fn abort_while_the_passkey_sheet_is_up_drops_its_late_assertion() {
    let _ports = PORTS.lock().await;
    let hold = Arc::new(Notify::new());
    let keys = Arc::new(FakePasskeys {
        result: Ok(json!({"id":"cred"})),
        options: Mutex::new(vec![]),
        entered: Arc::new(Notify::new()),
        hold: Some(hold.clone()),
    });
    let f = fixture(
        vec![("/api/passkey-primary/options", primary_options())],
        None,
        keys.clone(),
    );
    let client = f.client.clone();
    let pending = tokio::spawn(async move { act(&client, ClientAction::SignInWithPasskey).await });
    keys.entered.notified().await;
    assert!(act(&f.client, ClientAction::AbortSignIn).await.ok);
    hold.notify_one();
    let ended = pending.await.unwrap();
    assert_eq!(ended.error.unwrap().code, "stale_generation");
    assert!(f
        .http
        .seen
        .lock()
        .unwrap()
        .iter()
        .all(|request| request.path != "/api/passkey-primary/verify"));
    assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
}

fn redirect_port(browser: &Browser, index: usize) -> u16 {
    let url = browser.opened.lock().unwrap()[index].clone();
    Url::parse(&url.query_pairs().find(|(key, _)| key == "redirect_to").unwrap().1)
        .unwrap()
        .port()
        .unwrap()
}

/// The browser round trip holds no coordinator lock: runtime monitoring and the
/// updater keep running while a person signs in elsewhere.
#[tokio::test]
async fn a_browser_wait_leaves_runtime_monitoring_and_updates_running() {
    let _ports = PORTS.lock().await;
    let browser = browser(Browsing::Nothing);
    let f = fixture(
        vec![("/api/config/auth", config(true))],
        Some(browser.clone()),
        passkeys(Err("x")),
    );
    let client = f.client.clone();
    let pending = tokio::spawn(async move { act(&client, google()).await });
    browser.redirected.notified().await;
    tokio::time::timeout(Duration::from_secs(1), f.client.refresh_local())
        .await
        .expect("refresh_local ran during the browser wait");
    let update = act(&f.client, ClientAction::UpdateCheck {}).await;
    assert_ne!(update.error.map(|error| error.code).as_deref(), Some("busy"));
    assert!(act(&f.client, ClientAction::AbortSignIn).await.ok);
    let ended = tokio::time::timeout(Duration::from_secs(5), pending)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(ended.error.unwrap().code, "stale_generation");
}

/// One attempt owns sign-in at a time: a new attempt retires the waiting one,
/// whose port closes and whose late code can never commit.
#[tokio::test]
async fn a_new_attempt_retires_the_waiting_one_and_signs_in() {
    let _ports = PORTS.lock().await;
    let browser = browsers(vec![Browsing::Nothing, Browsing::Code("second-code")]);
    let f = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            (
                "/api/desktop/session",
                reply(200, signed_in(), Some("account=fixture; Path=/; Secure; HttpOnly")),
            ),
        ],
        Some(browser.clone()),
        passkeys(Err("x")),
    );
    let client = f.client.clone();
    let first = tokio::spawn(async move { act(&client, google()).await });
    browser.redirected.notified().await;
    let first_port = redirect_port(&browser, 0);
    let second = act(&f.client, google()).await;
    assert!(second.ok, "{:?}", second.error);
    assert_eq!(second.state.account.status, AccountStatus::SignedIn);
    let first = tokio::time::timeout(Duration::from_secs(5), first)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(first.error.unwrap().code, "stale_generation");
    assert!(TcpStream::connect(("127.0.0.1", first_port)).await.is_err());
    let seen = f.http.seen.lock().unwrap().clone();
    let tokens: Vec<_> = seen.iter().filter(|request| request.path == "/auth/v1/token").collect();
    assert_eq!(tokens.len(), 1);
    assert_eq!(tokens[0].body.clone().unwrap()["auth_code"], "second-code");
}

/// avibe.bot's public switches decide which ways in the page offers; a field
/// that is missing or false offers nothing.
#[tokio::test]
async fn sign_in_methods_follow_the_public_switches() {
    let _ports = PORTS.lock().await;
    let f = fixture(
        vec![(
            "/api/config/auth",
            reply(
                200,
                json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture",
                    "google_login_enabled":true,"github_login_enabled":false,"apple_login_enabled":true}),
                None,
            ),
        )],
        None,
        passkeys(Err("x")),
    );
    let before = serde_json::to_value(f.client.state()).unwrap();
    assert_eq!(before["sign_in"], json!({"providers":[],"passkey":false}));
    let refreshed = act(
        &f.client,
        serde_json::from_value(json!({"type":"refresh_sign_in_methods"})).unwrap(),
    )
    .await;
    assert!(refreshed.ok, "{:?}", refreshed.error);
    let after = serde_json::to_value(&refreshed.state).unwrap();
    assert_eq!(
        after["sign_in"],
        json!({"providers":["google","apple"],"passkey":false})
    );

    let g = fixture(
        vec![(
            "/api/config/auth",
            reply(
                200,
                json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture",
                    "google_login_enabled":false,"github_login_enabled":true,"apple_login_enabled":false,
                    "passkey_login_enabled":true}),
                None,
            ),
        )],
        None,
        passkeys(Err("x")),
    );
    let refreshed = act(
        &g.client,
        serde_json::from_value(json!({"type":"refresh_sign_in_methods"})).unwrap(),
    )
    .await;
    let after = serde_json::to_value(&refreshed.state).unwrap();
    assert_eq!(after["sign_in"], json!({"providers":["github"],"passkey":true}));
}

/// "Sign in with a different account" on an owed second factor ends that
/// attempt in place: signed out, the MFA session dropped, the window kept.
#[tokio::test]
async fn abort_on_an_owed_second_factor_starts_over_without_leaving() {
    let _ports = PORTS.lock().await;
    let f = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            ("/api/desktop/session", two_factor()),
        ],
        Some(browser(Browsing::Code("synthetic-code"))),
        passkeys(Err("x")),
    );
    assert_eq!(
        act(&f.client, google()).await.state.account.status,
        AccountStatus::SecondFactorRequired
    );
    let before = f.client.state().generation;
    let ended = act(&f.client, ClientAction::AbortSignIn).await;
    assert!(ended.ok, "{:?}", ended.error);
    assert_eq!(ended.state.account.status, AccountStatus::SignedOut);
    assert!(ended.state.generation > before);
    assert!(f.private.0.lock().unwrap().is_empty(), "nothing of the attempt is kept");
    // The ended attempt's second factor can no longer be answered: no request leaves.
    let calls = f.http.seen.lock().unwrap().len();
    let late = act(
        &f.client,
        ClientAction::VerifySecondFactor {
            method: CodeMethod::Totp,
            code: "123456".into(),
        },
    )
    .await;
    assert_eq!(late.error.unwrap().code, "invalid_action");
    assert_eq!(f.http.seen.lock().unwrap().len(), calls);
    assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
}

/// avibe.bot's passkey switch alone does not offer passkey sign-in on an OS
/// that has no passkey sheet.
#[tokio::test]
async fn passkey_sign_in_needs_the_switch_and_this_os() {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("Studio").unwrap();
    let state = ClientState::new("Studio".into(), "0.1.0".into(), json!({}));
    let http = Arc::new(Http {
        replies: Mutex::new(
            vec![(
                "/api/config/auth",
                reply(
                    200,
                    json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture",
                        "google_login_enabled":true,"passkey_login_enabled":true}),
                    None,
                ),
            )]
            .into(),
        ),
        seen: Mutex::new(vec![]),
        held: Mutex::new(None),
    });
    let client = Coordinator::new(
        state,
        record,
        store,
        http.clone(),
        Arc::new(Private::default()),
        Arc::new(NoPasskeySheet),
        Arc::new(View),
    );
    let refreshed = act(
        &client,
        serde_json::from_value(json!({"type":"refresh_sign_in_methods"})).unwrap(),
    )
    .await;
    let after = serde_json::to_value(&refreshed.state).unwrap();
    assert_eq!(after["sign_in"], json!({"providers":["google"],"passkey":false}));
    // Native refuses too, before anything starts: no request, no generation move.
    let calls = http.seen.lock().unwrap().len();
    let before = client.state();
    let refused = act(&client, ClientAction::SignInWithPasskey).await;
    assert_eq!(refused.error.unwrap().code, "passkey_unavailable");
    assert_eq!(http.seen.lock().unwrap().len(), calls);
    assert_eq!(client.state().generation, before.generation);
    assert_eq!(client.state().account.status, AccountStatus::SignedOut);
}

fn public(flags: Value) -> Response {
    let mut body = json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture"});
    body.as_object_mut().unwrap().extend(flags.as_object().unwrap().clone());
    reply(200, body, None)
}
fn sign_in(state: &ClientState) -> Value {
    serde_json::to_value(state).unwrap()["sign_in"].clone()
}
/// Holds the `nth` request (1-based) until released; returns (entered, release).
fn hold(http: &Http, nth: usize) -> (Arc<Notify>, Arc<Notify>) {
    let (entered, release) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
    *http.held.lock().unwrap() = Some((nth, entered.clone(), release.clone()));
    (entered, release)
}
fn password() -> ClientAction {
    ClientAction::SignInPassword {
        email: "user@example.test".into(),
        password: "synthetic-password".into(),
    }
}

/// Switches that are missing, or a read that fails, offer no way but email.
#[tokio::test]
async fn missing_or_unread_switches_offer_only_email() {
    let f = fixture(
        vec![
            ("/api/config/auth", public(json!({}))),
            ("/api/config/auth", reply(500, json!({"error":"unavailable"}), None)),
        ],
        None,
        passkeys(Err("x")),
    );
    let closed = json!({"providers":[],"passkey":false});
    assert_eq!(sign_in(&f.client.state()), closed, "nothing before the first read");
    let read = act(&f.client, ClientAction::RefreshSignInMethods).await;
    assert!(read.ok, "{:?}", read.error);
    assert_eq!(sign_in(&read.state), closed);
    let failed = act(&f.client, ClientAction::RefreshSignInMethods).await;
    assert!(!failed.ok);
    assert_eq!(sign_in(&f.client.state()), closed);
}

/// avibe.bot's public switches are not the account's: an account change
/// during the read (abort here; restore and local binding move it too)
/// does not drop them.
#[tokio::test]
async fn a_switch_read_survives_an_account_change() {
    let f = fixture(
        vec![(
            "/api/config/auth",
            public(json!({"google_login_enabled":true,"github_login_enabled":true,"passkey_login_enabled":true})),
        )],
        None,
        passkeys(Err("x")),
    );
    let (entered, release) = hold(&f.http, 1);
    let client = f.client.clone();
    let reading = tokio::spawn(async move { act(&client, ClientAction::RefreshSignInMethods).await });
    entered.notified().await;
    let before = f.client.state().generation;
    assert!(act(&f.client, ClientAction::AbortSignIn).await.ok);
    assert!(
        f.client.state().generation > before,
        "the account moved on during the read"
    );
    release.notify_one();
    let read = reading.await.unwrap();
    assert!(read.ok, "{:?}", read.error);
    assert_eq!(
        sign_in(&f.client.state()),
        json!({"providers":["google","github"],"passkey":true})
    );
}

/// An account operation that began before the switches were read keeps them
/// when it commits: here a password sign-in that lands on a second factor.
#[tokio::test]
async fn an_account_commit_keeps_switches_read_meanwhile() {
    let f = fixture(
        vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            (
                "/api/config/auth",
                public(json!({"google_login_enabled":true,"github_login_enabled":true,"passkey_login_enabled":true})),
            ),
            ("/api/desktop/session", two_factor()),
        ],
        None,
        passkeys(Err("x")),
    );
    let (entered, release) = hold(&f.http, 2);
    let client = f.client.clone();
    let signing = tokio::spawn(async move { act(&client, password()).await });
    entered.notified().await;
    let read = act(&f.client, ClientAction::RefreshSignInMethods).await;
    assert!(read.ok, "{:?}", read.error);
    let shown = json!({"providers":["google","github"],"passkey":true});
    assert_eq!(sign_in(&f.client.state()), shown);
    release.notify_one();
    let signed = signing.await.unwrap();
    assert_eq!(signed.state.account.status, AccountStatus::SecondFactorRequired);
    assert_eq!(sign_in(&signed.state), shown);
    assert_eq!(sign_in(&f.client.state()), shown);
}

/// Reads show in the order they started: a slow earlier read never replaces
/// a newer one.
#[tokio::test]
async fn switch_reads_show_in_the_order_they_started() {
    let f = fixture(
        vec![
            ("/api/config/auth", public(json!({"apple_login_enabled":true}))),
            ("/api/config/auth", public(json!({"google_login_enabled":true}))),
        ],
        None,
        passkeys(Err("x")),
    );
    let (entered, release) = hold(&f.http, 1);
    let client = f.client.clone();
    let earlier = tokio::spawn(async move { act(&client, ClientAction::RefreshSignInMethods).await });
    entered.notified().await;
    let newer = act(&f.client, ClientAction::RefreshSignInMethods).await;
    assert!(newer.ok, "{:?}", newer.error);
    let shown = json!({"providers":["google"],"passkey":false});
    assert_eq!(sign_in(&f.client.state()), shown);
    release.notify_one();
    assert!(earlier.await.unwrap().ok);
    assert_eq!(sign_in(&f.client.state()), shown);
}

/// A keychain that refuses every write.
struct NoKeychain;
impl Credentials for NoKeychain {
    fn read(&self, _: &str) -> Result<Option<Vec<u8>>, ClientError> {
        Ok(None)
    }
    fn write(&self, _: &str, _: &[u8]) -> Result<(), ClientError> {
        Err(ClientError::new("credentials_unavailable"))
    }
    fn erase(&self, _: &str) -> Result<(), ClientError> {
        Ok(())
    }
}

/// A sign-in whose save fails rolls back without taking the switches read
/// meanwhile with it.
#[tokio::test]
async fn a_rolled_back_save_keeps_switches_read_meanwhile() {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("Studio").unwrap();
    let http = Arc::new(Http {
        replies: Mutex::new(
            vec![
                ("/api/config/auth", config(true)),
                ("/auth/v1/token", tokens()),
                (
                    "/api/config/auth",
                    public(json!({"github_login_enabled":true,"passkey_login_enabled":true})),
                ),
                (
                    "/api/desktop/session",
                    reply(200, signed_in(), Some("account=fixture; Path=/; Secure; HttpOnly")),
                ),
            ]
            .into(),
        ),
        seen: Mutex::new(vec![]),
        held: Mutex::new(None),
    });
    let client = Arc::new(Coordinator::new(
        ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
        record,
        store,
        http.clone(),
        Arc::new(NoKeychain),
        passkeys(Err("x")),
        Arc::new(View),
    ));
    let (entered, release) = hold(&http, 2);
    let signing = {
        let client = client.clone();
        tokio::spawn(async move { act(&client, password()).await })
    };
    entered.notified().await;
    assert!(act(&client, ClientAction::RefreshSignInMethods).await.ok);
    let shown = json!({"providers":["github"],"passkey":true});
    release.notify_one();
    let signed = signing.await.unwrap();
    assert_eq!(
        signed.error.map(|error| error.code).as_deref(),
        Some("credentials_unavailable")
    );
    // The rollback that keeps a first factor's result readable, with its storage error.
    assert_eq!(
        client.state().enrollment.error_code.as_deref(),
        Some("credentials_unavailable")
    );
    assert_eq!(sign_in(&client.state()), shown);
}

/// Each account's second-factor session carries its own cookie.
fn factor_with(email: &str, methods: Value) -> Response {
    let cookie = format!("account={}; Path=/; Secure; HttpOnly", email.split('@').next().unwrap());
    reply(
        403,
        json!({"error":"two_factor_required","email":email,"gate":"challenge","methods":methods}),
        Some(&cookie),
    )
}
fn http_of(replies: Vec<(&'static str, Response)>) -> Arc<Http> {
    Arc::new(Http {
        replies: Mutex::new(replies.into()),
        seen: Mutex::new(vec![]),
        held: Mutex::new(None),
    })
}

/// A second factor on a system with no passkey sheet starts at what it can
/// run: native drops passkey from the published methods, and a direct passkey
/// verify sends nothing. Where the sheet runs, the order is avibe.bot's.
#[tokio::test]
async fn a_second_factor_offers_passkey_only_where_the_sheet_runs() {
    for (sheet, expected) in [(false, json!(["totp"])), (true, json!(["passkey", "totp"]))] {
        let root = tempfile::tempdir().unwrap();
        let store = ConnectionStore::new(root.path().join("connection.json"));
        let record = store.load("Studio").unwrap();
        let http = http_of(vec![
            ("/api/config/auth", config(true)),
            ("/auth/v1/token", tokens()),
            (
                "/api/desktop/session",
                factor_with("user@example.test", json!(["passkey", "totp"])),
            ),
        ]);
        let passkeys: Arc<dyn Passkeys> = if sheet {
            passkeys(Err("x"))
        } else {
            Arc::new(NoPasskeySheet)
        };
        let client = Coordinator::new(
            ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
            record,
            store,
            http.clone(),
            Arc::new(Private::default()),
            passkeys,
            Arc::new(View),
        );
        let signed = act(&client, password()).await;
        assert_eq!(signed.state.account.status, AccountStatus::SecondFactorRequired);
        assert_eq!(
            serde_json::to_value(&signed.state).unwrap()["account"]["methods"],
            expected,
            "sheet={sheet}"
        );
        if !sheet {
            let calls = http.seen.lock().unwrap().len();
            let refused = act(&client, ClientAction::VerifyPasskey).await;
            assert!(!refused.ok);
            assert_eq!(http.seen.lock().unwrap().len(), calls, "no challenge is requested");
            assert_eq!(client.state().account.status, AccountStatus::SecondFactorRequired);
        }
    }
}

/// An account restored from the keychain that owes its second factor.
struct Restored {
    client: Arc<Coordinator>,
    http: Arc<Http>,
    private: Arc<Private>,
    path: std::path::PathBuf,
    jar: Vec<u8>,
    _root: tempfile::TempDir,
}
async fn restored_owing_a_second_factor(after: Vec<(&'static str, Response)>) -> Restored {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("connection.json");
    let store = ConnectionStore::new(path.clone());
    let mut record = store.load("Studio").unwrap();
    record.account_id = Some("account-a".into());
    store.save(&record).unwrap();
    let mut replies = vec![("/api/mobile/session", factor_with("a@example.test", json!(["totp"])))];
    replies.extend(after);
    let http = http_of(replies);
    let jar = NativeSession::new("https://avibe.bot/".parse().unwrap(), http.clone())
        .persist()
        .unwrap();
    let private = Arc::new(Private::default());
    private.0.lock().unwrap().insert("account-a".into(), jar.clone());
    let client = Arc::new(Coordinator::new(
        ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
        record,
        store,
        http.clone(),
        private.clone(),
        passkeys(Err("x")),
        Arc::new(View),
    ));
    client.restore_account().await.unwrap();
    assert_eq!(client.state().account.status, AccountStatus::SecondFactorRequired);
    Restored {
        client,
        http,
        private,
        path,
        jar,
        _root: root,
    }
}
fn saved_account(path: &std::path::Path) -> Option<String> {
    ConnectionStore::new(path.to_path_buf())
        .load("Studio")
        .unwrap()
        .account_id
}

/// "Use a different account" on a restored account that owes its second
/// factor forgets it, as the iPhone signs out: its record and keychain entry
/// go, the window stays, and the next account owing a second factor is never
/// written under the old one's key. A restart restores nothing.
#[tokio::test]
async fn a_different_account_forgets_a_restored_account_owing_a_second_factor() {
    let r = restored_owing_a_second_factor(vec![
        ("/api/config/auth", config(true)),
        ("/auth/v1/token", tokens()),
        ("/api/desktop/session", factor_with("b@example.test", json!(["totp"]))),
    ])
    .await;
    let ended = act(&r.client, ClientAction::AbortSignIn).await;
    assert!(ended.ok, "{:?}", ended.error);
    assert_eq!(ended.state.account.status, AccountStatus::SignedOut);
    assert!(!r.private.0.lock().unwrap().contains_key("account-a"));
    assert_eq!(saved_account(&r.path), None);

    let b = act(&r.client, password()).await;
    assert_eq!(b.state.account.status, AccountStatus::SecondFactorRequired);
    assert!(
        r.private.0.lock().unwrap().is_empty(),
        "nothing is written under any key"
    );
    assert_eq!(saved_account(&r.path), None);

    let store = ConnectionStore::new(r.path.clone());
    let record = store.load("Studio").unwrap();
    let after = Coordinator::new(
        ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
        record,
        store,
        http_of(vec![]),
        r.private.clone(),
        passkeys(Err("x")),
        Arc::new(View),
    );
    after.restore_account().await.unwrap();
    assert_eq!(after.state().account.status, AccountStatus::SignedOut);
    let _ = (&r.http, &r.jar);
}

/// Erases fail for one key, as a keychain can.
struct StuckKey(Private, &'static str);
impl Credentials for StuckKey {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        self.0.read(id)
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        self.0.write(id, bytes)
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        if id == self.1 {
            return Err(ClientError::new("credentials_unavailable"));
        }
        self.0.erase(id)
    }
}

/// "Continue on this computer" from a restored account that owes its second
/// factor, then another sign-in:
/// - while the other account only owes its second factor, nothing is written
///   and the stored account stays for a later launch;
/// - once a different account signs in, the new record is saved first and
///   then the old account's key is erased, in the same credential owner;
/// - an erase that fails leaves the old key owned for a sign-out, says so,
///   and never puts the new account under the old key.
#[tokio::test]
async fn another_sign_in_retires_a_restored_account_only_once_it_signs_in() {
    let shown = |r: &Restored, key: &str| {
        r.private
            .0
            .lock()
            .unwrap()
            .get(key)
            .map(|jar| String::from_utf8_lossy(jar).into_owned())
    };
    // Owes its own second factor: the stored account is kept as it was.
    let r = restored_owing_a_second_factor(vec![
        ("/api/config/auth", config(true)),
        ("/auth/v1/token", tokens()),
        ("/api/desktop/session", factor_with("b@example.test", json!(["totp"]))),
    ])
    .await;
    let kept = shown(&r, "account-a");
    assert!(kept.as_deref().is_some_and(|jar| jar.contains("account=a;")));
    assert!(act(&r.client, ClientAction::ReturnLocal).await.ok);
    let b = act(&r.client, password()).await;
    assert_eq!(b.state.account.status, AccountStatus::SecondFactorRequired);
    assert_eq!(shown(&r, "account-a"), kept, "B's session is not written under A's key");
    assert_eq!(r.private.0.lock().unwrap().len(), 1, "nothing else is written");
    assert_eq!(
        saved_account(&r.path).as_deref(),
        Some("account-a"),
        "A stays for a later launch"
    );

    // Signs in: B's record and key, then A's key goes.
    let r = restored_owing_a_second_factor(vec![
        ("/api/config/auth", config(true)),
        ("/auth/v1/token", tokens()),
        (
            "/api/desktop/session",
            reply(200, signed_in(), Some("account=b; Path=/; Secure; HttpOnly")),
        ),
    ])
    .await;
    assert!(act(&r.client, ClientAction::ReturnLocal).await.ok);
    let b = act(&r.client, password()).await;
    assert_eq!(b.state.account.status, AccountStatus::SignedIn, "{:?}", b.error);
    assert_eq!(saved_account(&r.path).as_deref(), Some("account-xiaoming"));
    assert!(shown(&r, "account-xiaoming").is_some_and(|jar| jar.contains("account=b;")));
    assert_eq!(shown(&r, "account-a"), None, "the stored account's key is erased");
    // (No local runtime here, so enrollment reports its own error; never a keychain one.)
    assert_ne!(
        b.state.enrollment.error_code.as_deref(),
        Some("credentials_unavailable")
    );
}

#[tokio::test]
async fn a_failed_erase_of_the_retired_account_says_so_and_keeps_it_apart() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("connection.json");
    let store = ConnectionStore::new(path.clone());
    let mut record = store.load("Studio").unwrap();
    record.account_id = Some("account-a".into());
    store.save(&record).unwrap();
    let http = http_of(vec![
        ("/api/mobile/session", factor_with("a@example.test", json!(["totp"]))),
        ("/api/config/auth", config(true)),
        ("/auth/v1/token", tokens()),
        (
            "/api/desktop/session",
            reply(200, signed_in(), Some("account=b; Path=/; Secure; HttpOnly")),
        ),
    ]);
    let jar = NativeSession::new("https://avibe.bot/".parse().unwrap(), http.clone())
        .persist()
        .unwrap();
    let keychain = Arc::new(StuckKey(Private::default(), "account-a"));
    keychain.0 .0.lock().unwrap().insert("account-a".into(), jar);
    let client = Coordinator::new(
        ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
        record,
        store,
        http,
        keychain.clone(),
        passkeys(Err("x")),
        Arc::new(View),
    );
    client.restore_account().await.unwrap();
    assert!(act(&client, ClientAction::ReturnLocal).await.ok);
    let b = act(&client, password()).await;
    assert_eq!(b.state.account.status, AccountStatus::SignedIn, "{:?}", b.error);
    assert_eq!(
        client.state().enrollment.error_code.as_deref(),
        Some("credentials_unavailable"),
        "the failed erase is shown"
    );
    let keys = keychain.0 .0.lock().unwrap();
    assert!(keys
        .get("account-xiaoming")
        .is_some_and(|jar| String::from_utf8_lossy(jar).contains("account=b;")));
    assert!(
        keys.get("account-a")
            .is_some_and(|jar| !String::from_utf8_lossy(jar).contains("account=b;")),
        "B is never written under A's key"
    );
    assert_eq!(saved_account(&path).as_deref(), Some("account-xiaoming"));
}

/// A keychain whose writes and erases can be held at one key until the test
/// releases them, fail at one key, and are counted.
#[derive(Default)]
struct HeldKeychain {
    keys: Private,
    hold_write: Mutex<Option<(String, Hold)>>,
    hold_erase: Mutex<Option<(String, Hold)>>,
    fail_write: Mutex<Option<String>>,
    /// Erases of this key fail this many times, then succeed.
    fail_erase: Mutex<Option<(String, usize)>>,
    erased: Mutex<Vec<String>>,
}
/// Says when a held call arrives; the call waits until released.
struct Hold {
    entered: Arc<Notify>,
    release: std::sync::mpsc::Receiver<()>,
}
fn hold_at() -> (Hold, Arc<Notify>, std::sync::mpsc::Sender<()>) {
    let entered = Arc::new(Notify::new());
    let (release_tx, release) = std::sync::mpsc::channel();
    (
        Hold {
            entered: entered.clone(),
            release,
        },
        entered,
        release_tx,
    )
}
impl HeldKeychain {
    fn wait(slot: &Mutex<Option<(String, Hold)>>, id: &str) {
        let held = {
            let mut slot = slot.lock().unwrap();
            if slot.as_ref().is_some_and(|(key, _)| key == id) {
                slot.take()
            } else {
                None
            }
        };
        if let Some((_, hold)) = held {
            hold.entered.notify_one();
            let _ = hold.release.recv();
        }
    }
    fn jar(&self, id: &str) -> Option<String> {
        self.keys
            .0
            .lock()
            .unwrap()
            .get(id)
            .map(|jar| String::from_utf8_lossy(jar).into_owned())
    }
}
impl Credentials for HeldKeychain {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        self.keys.read(id)
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        Self::wait(&self.hold_write, id);
        if self.fail_write.lock().unwrap().as_deref() == Some(id) {
            return Err(ClientError::new("credentials_unavailable"));
        }
        self.keys.write(id, bytes)
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        Self::wait(&self.hold_erase, id);
        self.erased.lock().unwrap().push(id.to_owned());
        let mut fail = self.fail_erase.lock().unwrap();
        if let Some((key, left)) = fail.as_mut() {
            if key == id && *left > 0 {
                *left -= 1;
                return Err(ClientError::new("credentials_unavailable"));
            }
        }
        drop(fail);
        self.keys.erase(id)
    }
}

/// A stored account restored at launch, owing its second factor, then left
/// with "continue on this computer"; the next requests answer from `after`.
struct Left {
    client: Arc<Coordinator>,
    keychain: Arc<HeldKeychain>,
    path: std::path::PathBuf,
    _root: tempfile::TempDir,
}
async fn left_stored_account(id: &str, after: Vec<(&'static str, Response)>, local: bool) -> Left {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("connection.json");
    let store = ConnectionStore::new(path.clone());
    let mut record = store.load("Studio").unwrap();
    record.account_id = Some(id.into());
    store.save(&record).unwrap();
    let mut replies = vec![("/api/mobile/session", factor_with("a@example.test", json!(["totp"])))];
    replies.extend(after);
    let http = http_of(replies);
    let jar = NativeSession::new("https://avibe.bot/".parse().unwrap(), http.clone())
        .persist()
        .unwrap();
    let keychain = Arc::new(HeldKeychain::default());
    keychain.keys.0.lock().unwrap().insert(id.into(), jar);
    let client = Arc::new(Coordinator::new(
        ClientState::new("Studio".into(), "0.1.0".into(), json!({})),
        record,
        store,
        http,
        keychain.clone(),
        passkeys(Err("x")),
        Arc::new(View),
    ));
    client.restore_account().await.unwrap();
    assert_eq!(client.state().account.status, AccountStatus::SecondFactorRequired);
    if local {
        assert!(act(&client, ClientAction::ReturnLocal).await.ok);
    }
    Left {
        client,
        keychain,
        path,
        _root: root,
    }
}
fn b_signs_in() -> Vec<(&'static str, Response)> {
    vec![
        ("/api/config/auth", config(true)),
        ("/auth/v1/token", tokens()),
        (
            "/api/desktop/session",
            reply(200, signed_in(), Some("account=b; Path=/; Secure; HttpOnly")),
        ),
    ]
}
const B: &str = "account-xiaoming";
/// Runs `action` from another task and waits until it has moved the
/// generation (its fence), while a held keychain call is still waiting.
async fn fence_with(client: &Arc<Coordinator>, action: ClientAction) -> tokio::task::JoinHandle<ClientReply> {
    let before = client.state().generation;
    let racing = {
        let client = client.clone();
        tokio::spawn(async move { act(&client, action).await })
    };
    for _ in 0..200 {
        if client.state().generation > before {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(client.state().generation > before, "the racing action fenced");
    racing
}

/// Matrix 1 and 8: a fence or a failure before the commit rolls back with the
/// stored account untouched: its record and key stay, B's key goes, and
/// nothing of A is erased. (Pins; the old order passed these too.)
#[tokio::test]
async fn a_fence_or_failure_before_the_commit_leaves_the_stored_account_untouched() {
    let l = left_stored_account("account-a", b_signs_in(), true).await;
    let a = l.keychain.jar("account-a");
    let (hold, entered, release) = hold_at();
    *l.keychain.hold_write.lock().unwrap() = Some((B.into(), hold));
    let signing = {
        let client = l.client.clone();
        tokio::spawn(async move { act(&client, password()).await })
    };
    entered.notified().await;
    let local = fence_with(&l.client, ClientAction::ReturnLocal).await;
    release.send(()).unwrap();
    assert!(!signing.await.unwrap().ok);
    assert!(local.await.unwrap().ok);
    assert_eq!(saved_account(&l.path).as_deref(), Some("account-a"));
    assert_eq!(l.keychain.jar("account-a"), a);
    assert_eq!(l.keychain.jar(B), None);
    assert!(l.keychain.erased.lock().unwrap().iter().all(|id| id != "account-a"));
    assert_ne!(l.client.state().account.status, AccountStatus::SignedIn);

    let l = left_stored_account("account-a", b_signs_in(), true).await;
    let a = l.keychain.jar("account-a");
    *l.keychain.fail_write.lock().unwrap() = Some(B.into());
    let failed = act(&l.client, password()).await;
    assert_eq!(
        failed.error.map(|error| error.code).as_deref(),
        Some("credentials_unavailable")
    );
    assert_eq!(saved_account(&l.path).as_deref(), Some("account-a"));
    assert_eq!(l.keychain.jar("account-a"), a);
    assert_eq!(l.keychain.jar(B), None);
    assert!(l.keychain.erased.lock().unwrap().iter().all(|id| id != "account-a"));
}

/// Matrix 2 (red before the fix): the old key is erased only after B is
/// committed, so a fence that lands while that erase runs no longer rolls B
/// back onto a record that names A with A's key already gone.
#[tokio::test]
async fn the_old_key_is_retired_only_after_the_new_account_is_committed() {
    let l = left_stored_account("account-a", b_signs_in(), true).await;
    let (hold, entered, release) = hold_at();
    *l.keychain.hold_erase.lock().unwrap() = Some(("account-a".into(), hold));
    let signing = {
        let client = l.client.clone();
        tokio::spawn(async move { act(&client, password()).await })
    };
    entered.notified().await;
    let local = fence_with(&l.client, ClientAction::ReturnLocal).await;
    release.send(()).unwrap();
    let signed = signing.await.unwrap();
    assert!(local.await.unwrap().ok);
    if saved_account(&l.path).as_deref() == Some("account-a") {
        assert!(
            l.keychain.jar("account-a").is_some(),
            "a record that names A keeps A's key"
        );
    }
    assert!(signed.ok, "{:?}", signed.error);
    assert_eq!(l.client.state().account.status, AccountStatus::SignedIn);
    assert_eq!(saved_account(&l.path).as_deref(), Some(B));
    assert!(l.keychain.jar(B).is_some_and(|jar| jar.contains("account=b;")));
    assert_eq!(l.keychain.jar("account-a"), None);
}

/// Matrix 3: a sign-out while the old key is being erased waits for it, then
/// erases B; the signed-out state never shows the retirement's outcome.
#[tokio::test]
async fn a_sign_out_during_the_retirement_waits_and_then_erases_everything() {
    let l = left_stored_account("account-a", b_signs_in(), true).await;
    let (hold, entered, release) = hold_at();
    *l.keychain.hold_erase.lock().unwrap() = Some(("account-a".into(), hold));
    *l.keychain.fail_erase.lock().unwrap() = Some(("account-a".into(), 1));
    let signing = {
        let client = l.client.clone();
        tokio::spawn(async move { act(&client, password()).await })
    };
    entered.notified().await;
    let out = fence_with(&l.client, ClientAction::SignOut { intent_id: None }).await;
    assert_eq!(l.client.state().account.status, AccountStatus::SignedOut);
    release.send(()).unwrap();
    let _ = signing.await.unwrap();
    assert!(out.await.unwrap().ok);
    assert_ne!(
        l.client.state().enrollment.error_code.as_deref(),
        Some("credentials_unavailable"),
        "a failed retirement is not shown on the signed-out state"
    );
    assert!(
        l.keychain.keys.0.lock().unwrap().is_empty(),
        "B, and A left owned, are erased"
    );
    assert_eq!(saved_account(&l.path), None);
}

/// Matrix 4: an erase that fails leaves B committed under its own key, shows
/// the error, and keeps A owned, so a sign-out in this run erases it.
#[tokio::test]
async fn a_failed_retirement_shows_and_a_sign_out_still_erases_the_old_key() {
    let l = left_stored_account("account-a", b_signs_in(), true).await;
    *l.keychain.fail_erase.lock().unwrap() = Some(("account-a".into(), 1));
    let signed = act(&l.client, password()).await;
    assert!(signed.ok, "{:?}", signed.error);
    assert_eq!(signed.state.account.status, AccountStatus::SignedIn);
    assert_eq!(
        signed.state.enrollment.error_code.as_deref(),
        Some("credentials_unavailable")
    );
    assert_eq!(saved_account(&l.path).as_deref(), Some(B));
    assert!(l
        .keychain
        .jar("account-a")
        .is_some_and(|jar| !jar.contains("account=b;")));
    assert!(act(&l.client, ClientAction::SignOut { intent_id: None }).await.ok);
    assert!(l.keychain.keys.0.lock().unwrap().is_empty());
}

/// Matrix 5: the stored account finishing its own second factor is no
/// retirement: its key is rewritten, never erased.
#[tokio::test]
async fn the_same_account_finishing_its_second_factor_erases_nothing() {
    let l = left_stored_account(
        B,
        vec![
            ("/api/two-factor/verify", reply(200, json!({}), None)),
            (
                "/api/mobile/session",
                reply(200, signed_in(), Some("account=done; Path=/; Secure; HttpOnly")),
            ),
        ],
        false,
    )
    .await;
    let done = act(
        &l.client,
        ClientAction::VerifySecondFactor {
            method: CodeMethod::Totp,
            code: "123456".into(),
        },
    )
    .await;
    assert!(done.ok, "{:?}", done.error);
    assert_eq!(done.state.account.status, AccountStatus::SignedIn);
    assert!(l.keychain.erased.lock().unwrap().is_empty());
    assert!(l.keychain.jar(B).is_some_and(|jar| jar.contains("account=done;")));
    assert_eq!(saved_account(&l.path).as_deref(), Some(B));
}
