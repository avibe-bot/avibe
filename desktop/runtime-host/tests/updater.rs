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

#[test]
fn release_assets_come_from_the_mirror_first_by_prefix_substitution_only() {
    use avibe_runtime_host::update::{asset_url, sources};
    let url = asset_url("gh-v3.1.2rc7", "Avibe_3.1.2-rc.7_aarch64-apple-darwin.app.tar.gz");
    assert_eq!(
        sources(&url),
        [
            "https://dl.avibe.bot/releases/gh-v3.1.2rc7/Avibe_3.1.2-rc.7_aarch64-apple-darwin.app.tar.gz".to_owned(),
            url
        ]
    );
    for (github, mirror) in [
        ("v1.0.0/a%2Bb.tgz", "v1.0.0/a%2Bb.tgz"),
        ("v1.0.0/%E6%B5%8B%E8%AF%95.tgz", "v1.0.0/%E6%B5%8B%E8%AF%95.tgz"),
        ("v1.0.0/测试 a.tgz", "v1.0.0/测试 a.tgz"),
    ] {
        let url = format!("https://github.com/avibe-bot/avibe/releases/download/{github}");
        assert_eq!(sources(&url), [format!("https://dl.avibe.bot/releases/{mirror}"), url]);
    }
    for other in [
        "https://github.com/avibe-bot/avault/releases/download/v1.0.0/a.tgz",
        "https://api.github.com/repos/avibe-bot/avibe/releases",
        "http://github.com/avibe-bot/avibe/releases/download/v1.0.0/a.tgz",
    ] {
        assert_eq!(sources(other), [other]);
    }
}

#[test]
fn the_published_index_selects_the_highest_release_in_each_channel() {
    use avibe_runtime_host::update::index_releases;
    let releases = index_releases(&fs::read(fixtures().join("releases-index.json")).unwrap()).unwrap();
    let (version, test) = Channel::Test.latest(releases.clone()).unwrap();
    assert_eq!(version.to_string(), "3.1.2-rc.7");
    assert_eq!(test.tag, "gh-v3.1.2rc7");
    assert_eq!(test.commit.as_deref(), Some("1264c311df1088a8eb1efe46611ae72f5af51188"));
    for name in [
        "desktop-update-aarch64-apple-darwin.json",
        "desktop-update-aarch64-apple-darwin.json.sig",
        "Avibe_3.1.2-rc.7_aarch64-apple-darwin.app.tar.gz",
    ] {
        assert!(test.assets.iter().any(|asset| asset == name), "{name}");
    }
    // Runtime releases are full releases too, but never stable desktop tags.
    let (version, stable) = Channel::Stable.latest(releases).unwrap();
    assert_eq!((version.to_string().as_str(), stable.tag.as_str()), ("3.1.1", "v3.1.1"));
}

#[test]
fn channel_selection_uses_semver_and_the_prerelease_flag_together() {
    use avibe_runtime_host::update::Release;
    let release = |tag: &str, prerelease: bool| Release {
        tag: tag.into(),
        prerelease,
        commit: None,
        assets: Vec::new(),
    };
    let listed = vec![
        release("gh-v3.1.2rc9", true),
        release("gh-v3.1.2rc10", true),
        release("gh-v3.1.3rc01", true),
        release("gh-v9.0.0rc1", false),
        release("v9.0.0", true),
        release("v3.1.1", false),
    ];
    assert_eq!(Channel::Test.latest(listed.clone()).unwrap().1.tag, "gh-v3.1.2rc10");
    assert_eq!(Channel::Stable.latest(listed).unwrap().1.tag, "v3.1.1");
    assert!(Channel::Stable.latest(Vec::new()).is_none());
}

#[test]
fn only_the_supported_index_schema_for_this_repository_is_read() {
    use avibe_runtime_host::update::index_releases;
    let index = serde_json::json!({
        "schema_version": 1,
        "repository": "avibe-bot/avibe",
        "future_field": true,
        "releases": [{
            "tag": "v3.1.1",
            "prerelease": false,
            "published_at": "2026-09-01T00:00:00Z",
            "commit": "a".repeat(40),
            "assets": [{ "name": "测试.tgz", "size": 1, "sha256": "0".repeat(64), "future_field": 1 }]
        }]
    });
    let releases = index_releases(&serde_json::to_vec(&index).unwrap()).unwrap();
    assert_eq!(releases[0].assets, ["测试.tgz"]);
    for (field, value) in [
        ("schema_version", serde_json::json!(2)),
        ("repository", serde_json::json!("someone/avibe")),
        ("releases", serde_json::json!({})),
    ] {
        let mut changed = index.clone();
        changed[field] = value;
        assert!(
            index_releases(&serde_json::to_vec(&changed).unwrap()).is_err(),
            "{field}"
        );
    }
    assert!(index_releases(b"<html>captive portal</html>").is_err());
}
