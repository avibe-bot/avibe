use super::*;
use avibe_runtime_host::instance_client::transport::{NativeSession, Request, Response, Transport};
use std::{
    path::{Path, PathBuf},
    process::Command,
};

const CHILD_CASE: &str = "instance_credentials::windows::tests::interrupted_staging_child";
const LIVE_CASE: &str = "instance_credentials::windows::tests::live_credential_peer";
const FIXTURE_SYNC: &str = "AVIBE_TEST_CREDENTIAL_RENDEZVOUS";
const FIXTURE_PROFILE: &str = "AVIBE_TEST_CREDENTIAL_FIXTURE_PROFILE";
const TEST_APP: &str = "org.avibe.test.accountjar";
const ACCOUNT: &str = "synthetic-account";

struct NoNetwork;
#[async_trait::async_trait]
impl Transport for NoNetwork {
    async fn send(&self, _: Request) -> std::result::Result<Response, ClientError> {
        panic!("offline credential decoding must not issue a request")
    }
}

// Only control markers cross this private temporary directory; jars always go through WinCred.
fn wait_for(dir: &Path, mark: &str) {
    let deadline = Instant::now() + Duration::from_secs(45);
    while !dir.join(mark).is_file() {
        assert!(Instant::now() < deadline, "credential fixture never reached {mark}");
        std::thread::sleep(Duration::from_millis(25));
    }
}
fn signal(dir: &Path, mark: &str) {
    fs::write(dir.join(mark), []).unwrap();
}

#[test]
#[ignore = "peer of the isolated live Windows credential contention test"]
fn live_credential_peer() {
    let profile = uuid::Uuid::parse_str(&std::env::var(FIXTURE_PROFILE).expect("fixture profile")).unwrap();
    let owner = NativeCredentials::new(TEST_APP, &profile.to_string());
    let dir = PathBuf::from(std::env::var_os(FIXTURE_SYNC).expect("fixture rendezvous"));
    // Parent holds a real Change (the same lock restore needs), but remains alive throughout.
    let start = Instant::now();
    assert!(owner.read(ACCOUNT).is_err_and(|e| e.code == "credentials_unavailable"));
    assert!(start.elapsed() >= MAX_LOCK_WAIT);
    signal(&dir, "timed-out");
    wait_for(&dir, "published");
    assert!(owner.read(ACCOUNT).unwrap().as_deref() == Some(&vec![b'p'; CHUNK + 1][..]));
    owner.write(ACCOUNT, &vec![b'c'; CHUNK + 2]).unwrap();
    signal(&dir, "overwritten");
    wait_for(&dir, "erased");
    assert!(owner.read(ACCOUNT).unwrap().is_none());
    signal(&dir, "observed-absence");
}

fn exercise_live_contention(owner: &Arc<NativeCredentials>, profile: &str) {
    let dir = tempfile::tempdir().unwrap();
    let mut held = owner.clone().begin_write(ACCOUNT).unwrap();
    let mut peer = Command::new(std::env::current_exe().unwrap())
        .stdout(std::process::Stdio::null())
        .args([LIVE_CASE, "--ignored", "--exact"])
        .env(FIXTURE_PROFILE, profile)
        .env(FIXTURE_SYNC, dir.path())
        .spawn()
        .unwrap();
    wait_for(dir.path(), "timed-out");
    held.publish(&vec![b'p'; CHUNK + 1]).unwrap();
    drop(held);
    signal(dir.path(), "published");
    wait_for(dir.path(), "overwritten");
    assert!(owner.read(ACCOUNT).unwrap().as_deref() == Some(&vec![b'c'; CHUNK + 2][..]));
    owner.erase(ACCOUNT).unwrap();
    signal(dir.path(), "erased");
    wait_for(dir.path(), "observed-absence");
    assert!(peer.wait().unwrap().success());
}

// Invoked only by the lifecycle test. Always construct the isolated test namespace ourselves.
#[test]
#[ignore = "worker for the isolated Windows Credential Manager test"]
fn interrupted_staging_child() {
    let profile = uuid::Uuid::parse_str(&std::env::var(FIXTURE_PROFILE).expect("fixture profile")).unwrap();
    let owner = NativeCredentials::new("org.avibe.test.accountjar", &profile.to_string());
    let change = Change::begin(&owner, "synthetic-account").unwrap();
    change.scope.stage(change.prior(), &vec![b'q'; MAX_JAR]).unwrap();
    // Model process loss: skip Drop (including the file lock) after staging, before publishing.
    std::process::exit(73);
}

// Only explicitly selected on the native Windows runner. No production service/profile/id is used.
#[test]
#[ignore = "isolated Windows Credential Manager and DPAPI integration"]
fn windows_native_account_jar_lifecycle() {
    let profile = uuid::Uuid::new_v4().to_string();
    let app = "org.avibe.test.accountjar";
    let account = "synthetic-account";
    let owner = Arc::new(NativeCredentials::new(app, &profile));
    struct Cleanup(Arc<NativeCredentials>);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = self.0.erase("synthetic-account");
        }
    }
    let _cleanup = Cleanup(owner.clone());
    let released = owner.entry(account).unwrap();
    let read = || NativeCredentials::new(app, &profile).read(account);
    let origin = url::Url::parse("https://account.example/").unwrap();
    let mut session = NativeSession::new(origin.clone(), Arc::new(NoNetwork));
    session.store_cookies(
        vec![cookie::Cookie::parse("ordinary=fixture; Path=/; Secure; HttpOnly; Max-Age=3600").unwrap()],
        &origin,
    );
    let mut small = session.persist().unwrap();
    assert!(small.len() <= CHUNK);
    small.resize(CHUNK, b' '); // valid JSON at the exact released WinCred limit
                               // Exercise the released keyring read and real NativeSession restore path, not a UTF-8 proxy.
                               // The released cookie_store decoder runs before the new memory-only cookie filter.
    let legacy_restore =
        || NativeSession::restore(origin.clone(), Arc::new(NoNetwork), &released.get_secret().unwrap());
    let first = vec![b'a'; CHUNK + 1];
    let second: Vec<_> = (0..MAX_JAR).map(|i| b'0' + (i % 10) as u8).collect();
    // Released-format upgrade, real store across handles, inclusive size boundaries, no byte dumps.
    released.set_secret(&small).unwrap();
    assert!(legacy_restore().unwrap().cookie_value("ordinary").as_deref() == Some("fixture"));
    assert!(read().unwrap().as_deref() == Some(small.as_slice()));
    owner.write(account, &first).unwrap();
    assert!(read().unwrap().as_deref() == Some(first.as_slice()));
    let manifest = released.get_secret().unwrap();
    assert!(legacy_restore().is_err_and(|e| e.code == "credentials_unavailable"));
    let status = Command::new(std::env::current_exe().unwrap())
        .stdout(std::process::Stdio::null())
        .args([CHILD_CASE, "--ignored", "--exact"])
        .env(FIXTURE_PROFILE, &profile)
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(73)); // a filtered/ignored child cannot satisfy the crash test
    assert!(read().unwrap().as_deref() == Some(first.as_slice()));
    let mut change = owner.clone().begin_write(account).unwrap();
    change.publish(&second).unwrap();
    change.rollback().unwrap(); // failed connection-record commit restores the exact old root
    assert!(released.get_secret().unwrap() == manifest);
    assert!(read().unwrap().as_deref() == Some(first.as_slice()));
    owner.write(account, &second).unwrap();
    assert!(read().unwrap().as_deref() == Some(second.as_slice()));
    assert!(owner.write(account, &vec![b'x'; MAX_JAR + 1]).is_err());
    assert!(read().unwrap().as_deref() == Some(second.as_slice()));
    {
        let scope = Scope::open(&owner, account).unwrap();
        let m = Manifest::parse(&released.get_secret().unwrap()).unwrap();
        delete(&scope.chunk(m.bank, 0).unwrap()).unwrap(); // intact root, its bank did not roam
    }
    assert!(read().is_err_and(|e| e.code == "credentials_unavailable"));
    let mut signin = owner.clone().begin_write(account).unwrap();
    signin.publish(&first).unwrap();
    drop(signin);
    assert!(read().unwrap().as_deref() == Some(first.as_slice()));
    let mut m = Manifest::parse(&released.get_secret().unwrap()).unwrap();
    m.nonce[0] ^= 1; // force real CryptUnprotectData failure without changing any Windows password
    released.set_secret(&m.encode()).unwrap();
    assert!(read().is_err_and(|e| e.code == "credentials_unavailable"));
    owner.write(account, &small).unwrap();
    assert!(released.get_secret().unwrap() == small);
    owner.write(account, &second).unwrap();
    released.set_secret(&small).unwrap(); // released App overwrites, then signs out
    assert!(read().unwrap().as_deref() == Some(small.as_slice()));
    released.delete_credential().unwrap();
    assert!(read().unwrap().is_none());
    exercise_live_contention(&owner, &profile);
    owner.write(account, &first).unwrap();
    {
        let scope = Scope::open(&owner, account).unwrap();
        let bank = Manifest::parse(&released.get_secret().unwrap()).unwrap().bank;
        scope
            .erase_and_sweep(|b, i| {
                if b == bank && i == 0 {
                    Err(unavailable())
                } else {
                    delete(&scope.chunk(b, i)?)
                }
            })
            .unwrap();
        assert!(read_entry(&scope.chunk(bank, 0).unwrap()).unwrap().is_some());
        assert!(scope.read().unwrap().is_none()); // failure left ciphertext, not a session/error
    }
    owner.erase(account).unwrap(); // retries cleanup even when the root is already absent
    assert!(read().unwrap().is_none());
    assert!(matches!(released.get_secret(), Err(keyring::Error::NoEntry)));
    let scope = Scope::open(&owner, account).unwrap();
    for bank in 0..2 {
        for i in 0..SLOTS {
            assert!(read_entry(&scope.chunk(bank, i).unwrap()).unwrap().is_none());
        }
    }
}
