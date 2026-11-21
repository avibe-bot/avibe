use super::*;
use std::process::Command;

const CHILD_CASE: &str = "instance_credentials::windows::tests::interrupted_staging_child";
const FIXTURE_PROFILE: &str = "AVIBE_TEST_CREDENTIAL_FIXTURE_PROFILE";

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
    let small = vec![b's'; CHUNK];
    let first = vec![b'a'; CHUNK + 1];
    let second: Vec<_> = (0..MAX_JAR).map(|i| b'0' + (i % 10) as u8).collect();
    // Released-format upgrade, real store across handles, inclusive size boundaries, no byte dumps.
    released.set_secret(&small).unwrap();
    assert!(read().unwrap().as_deref() == Some(small.as_slice()));
    owner.write(account, &first).unwrap();
    assert!(read().unwrap().as_deref() == Some(first.as_slice()));
    let manifest = released.get_secret().unwrap();
    assert!(std::str::from_utf8(&manifest).is_err()); // old JSON reader cannot consume this format
    let status = Command::new(std::env::current_exe().unwrap())
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
    owner.write(account, &first).unwrap();
    owner.erase(account).unwrap();
    assert!(read().unwrap().is_none());
    assert!(matches!(released.get_secret(), Err(keyring::Error::NoEntry)));
    let scope = Scope::open(&owner, account).unwrap();
    for bank in 0..2 {
        for i in 0..SLOTS {
            assert!(read_entry(&scope.chunk(bank, i).unwrap()).unwrap().is_none());
        }
    }
}
