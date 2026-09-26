use avibe_runtime_host::update::{Artifact, Channel, Manifest, TARGETS};
use std::{fs, path::PathBuf};
fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/updater")
}
fn public_key() -> String {
    fs::read_to_string(fixtures().join("public-key.txt")).unwrap()
}
fn authenticated(channel: &str, target: &str) -> Manifest {
    let name = format!("{channel}-{target}.json");
    Manifest::authenticated(
        &fs::read(fixtures().join(&name)).unwrap(),
        &fs::read_to_string(fixtures().join(format!("{name}.sig"))).unwrap(),
        &public_key(),
    )
    .unwrap()
}
#[test]
fn real_tauri_signatures_authenticate_both_channels_and_every_platform() {
    for (channel, name, tag) in [
        (Channel::Test, "test", "gh-v3.1.2rc15"),
        (Channel::Stable, "stable", "v3.1.2"),
    ] {
        for (target, _, _) in TARGETS {
            let manifest = authenticated(name, target);
            let artifact = manifest.validate(channel, tag, &"a".repeat(40), target).unwrap();
            artifact
                .verify(&fs::read(fixtures().join("artifact.bin")).unwrap(), &public_key())
                .unwrap();
            let wrong_channel = if channel == Channel::Test {
                Channel::Stable
            } else {
                Channel::Test
            };
            assert!(manifest.validate(wrong_channel, tag, &"a".repeat(40), target).is_err());
            assert!(manifest.validate(channel, tag, &"b".repeat(40), target).is_err());
            assert!(manifest
                .validate(channel, "gh-v3.1.2rc16", &"a".repeat(40), target)
                .is_err());
            for (other, _, _) in TARGETS.iter().filter(|(other, _, _)| other != target) {
                assert!(manifest.validate(channel, tag, &"a".repeat(40), other).is_err());
            }
        }
    }
}
#[test]
fn tampering_with_any_signed_manifest_byte_is_rejected() {
    let raw = fs::read(fixtures().join("test-aarch64-apple-darwin.json")).unwrap();
    let signature = fs::read_to_string(fixtures().join("test-aarch64-apple-darwin.json.sig")).unwrap();
    for index in 0..raw.len() {
        let mut altered = raw.clone();
        altered[index] ^= 1;
        assert!(
            Manifest::authenticated(&altered, &signature, &public_key()).is_err(),
            "byte {index}"
        );
    }
}
#[test]
fn payload_signature_and_integrity_are_independent_install_gates() {
    let manifest = authenticated("test", "aarch64-apple-darwin");
    let artifact = manifest.platforms.values().next().unwrap();
    let data = fs::read(fixtures().join("artifact.bin")).unwrap();
    assert!(artifact.verify(b"wrong payload", &public_key()).is_err());
    assert!(Artifact {
        signature: "app-adhoc".into(),
        ..artifact.clone()
    }
    .verify(&data, &public_key())
    .is_err());
    assert!(Artifact {
        sha256: "0".repeat(64),
        ..artifact.clone()
    }
    .verify(&data, &public_key())
    .is_err());
    assert!(Artifact {
        size: 1,
        ..artifact.clone()
    }
    .verify(&data, &public_key())
    .is_err());
}
#[test]
fn semantic_mismatches_cannot_be_hidden_by_valid_json() {
    let original = authenticated("test", "aarch64-apple-darwin");
    for field in [
        "schema_version",
        "repository",
        "tag",
        "source_sha",
        "channel",
        "version",
        "target",
        "platforms",
    ] {
        let mut value = serde_json::to_value(&original).unwrap();
        value[field] = serde_json::json!("invalid");
        if let Ok(manifest) = serde_json::from_value::<Manifest>(value) {
            assert!(
                manifest
                    .validate(Channel::Test, "gh-v3.1.2rc15", &"a".repeat(40), "aarch64-apple-darwin")
                    .is_err(),
                "{field}"
            );
        }
    }
}

#[test]
fn download_and_verification_failures_never_reach_the_replacement_boundary() {
    use avibe_runtime_host::update::install_verified;
    let manifest = authenticated("stable", "x86_64-pc-windows-msvc");
    let artifact = manifest.platforms.values().next().unwrap();
    for download in [Err("network failed".into()), Ok(b"tampered payload".to_vec())] {
        let result = install_verified(download, artifact, &public_key(), |_| -> Result<(), String> {
            panic!("unverified download reached the installer")
        });
        assert!(result.is_err());
    }
    let payload = fs::read(fixtures().join("artifact.bin")).unwrap();
    let result = install_verified(Ok(payload.clone()), artifact, &public_key(), |data| {
        assert_eq!(data, payload);
        Err::<(), String>("replacement failure".into())
    });
    assert_eq!(result, Err("replacement failure".into()));
}
