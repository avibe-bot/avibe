//! Signed desktop release contract, shared by the shell and release verifier.
use base64::{engine::general_purpose::STANDARD, Engine};
use minisign_verify::{PublicKey, Signature};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const REPOSITORY: &str = "avibe-bot/avibe";
pub const TARGETS: &[(&str, &str, &str)] = &[
    ("aarch64-apple-darwin", "darwin-aarch64", ".app.tar.gz"),
    ("x86_64-apple-darwin", "darwin-x86_64", ".app.tar.gz"),
    ("x86_64-pc-windows-msvc", "windows-x86_64", ".exe"),
];

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Channel {
    Test,
    Stable,
}

impl Channel {
    pub fn for_version(version: &Version) -> Self {
        if version.pre.is_empty() {
            Self::Stable
        } else {
            Self::Test
        }
    }
    pub fn version(self, tag: &str) -> Result<Version, String> {
        let raw = match self {
            Self::Stable => tag.strip_prefix('v').ok_or("stable tag required")?.to_string(),
            Self::Test => {
                let (core, rc) = tag
                    .strip_prefix("gh-v")
                    .ok_or("TEST tag required")?
                    .split_once("rc")
                    .ok_or("canonical rc tag required")?;
                if rc.is_empty() || !rc.bytes().all(|c| c.is_ascii_digit()) || (rc.len() > 1 && rc.starts_with('0')) {
                    return Err("invalid rc number".into());
                }
                format!("{core}-rc.{rc}")
            }
        };
        let version = Version::parse(&raw).map_err(|_| "invalid version")?;
        if !version.build.is_empty()
            || match self {
                Self::Stable => !version.pre.is_empty(),
                Self::Test => version
                    .pre
                    .to_string()
                    .strip_prefix("rc.")
                    .is_none_or(|rc| !rc.bytes().all(|c| c.is_ascii_digit())),
            }
        {
            return Err("noncanonical release tag".into());
        }
        Ok(version)
    }
    /// The highest SemVer release in this channel; publication order never counts.
    pub fn latest(self, releases: Vec<Release>) -> Option<(Version, Release)> {
        releases
            .into_iter()
            .filter(|release| release.prerelease == (self == Self::Test))
            .filter_map(|release| Some((self.version(&release.tag).ok()?, release)))
            .max_by(|a, b| a.0.cmp(&b.0))
    }
}

pub fn target_spec(target: &str) -> Result<(&'static str, &'static str), String> {
    TARGETS
        .iter()
        .find(|(name, _, _)| *name == target)
        .map(|(_, key, suffix)| (*key, *suffix))
        .ok_or("unsupported target".into())
}
pub fn manifest_name(target: &str) -> String {
    format!("desktop-update-{target}.json")
}
pub fn asset_url(tag: &str, name: &str) -> String {
    format!("https://github.com/{REPOSITORY}/releases/download/{tag}/{name}")
}
/// Byte-identical copies of published release assets, plus the release index
/// (`docs/plans/release-download-mirror.md`). Unauthenticated, like GitHub:
/// every byte from either source passes the same verification.
pub const MIRROR: &str = "https://dl.avibe.bot";
pub const INDEX_URL: &str = "https://dl.avibe.bot/index/releases.json";
/// Download order for a release asset URL: the mirror, then GitHub. The mirror
/// URL is derived only by substituting the prefix, keeping percent-encoding.
pub fn sources(url: &str) -> Vec<String> {
    let github = format!("https://github.com/{REPOSITORY}/releases/download/");
    match url.strip_prefix(&github) {
        Some(path) => vec![format!("{MIRROR}/releases/{path}"), url.to_owned()],
        None => vec![url.to_owned()],
    }
}

/// A published release as discovery sees it, from the index or the GitHub API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Release {
    pub tag: String,
    pub prerelease: bool,
    /// The peeled tag commit, when the source already lists it.
    pub commit: Option<String>,
    /// Asset names published under the canonical download URL.
    pub assets: Vec<String>,
}
#[derive(Deserialize)]
struct Index {
    schema_version: u32,
    repository: String,
    releases: Vec<IndexRelease>,
}
#[derive(Deserialize)]
struct IndexRelease {
    tag: String,
    prerelease: bool,
    commit: String,
    assets: Vec<IndexAsset>,
}
#[derive(Deserialize)]
struct IndexAsset {
    name: String,
}
/// Parses the mirror's release index, schema 1. Unknown fields are ignored so
/// the producer can add some without breaking installed shells.
pub fn index_releases(bytes: &[u8]) -> Result<Vec<Release>, String> {
    let index: Index = serde_json::from_slice(bytes).map_err(|_| "invalid release index")?;
    if index.schema_version != 1 || index.repository != REPOSITORY {
        return Err("unsupported release index".into());
    }
    Ok(index
        .releases
        .into_iter()
        .map(|release| Release {
            tag: release.tag,
            prerelease: release.prerelease,
            commit: Some(release.commit),
            assets: release.assets.into_iter().map(|asset| asset.name).collect(),
        })
        .collect())
}
pub fn artifact_name(version: &str, target: &str) -> Result<String, String> {
    Ok(format!("Avibe_{version}_{target}{}", target_spec(target)?.1))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub url: String,
    pub signature: String,
    pub sha256: String,
    pub size: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema_version: u32,
    pub repository: String,
    pub tag: String,
    pub source_sha: String,
    pub channel: Channel,
    pub version: String,
    pub target: String,
    pub platforms: BTreeMap<String, Artifact>,
}

pub fn verify_signature(bytes: &[u8], signature: &str, public_key: &str) -> Result<(), String> {
    let decode = |s: &str| -> Result<String, String> {
        String::from_utf8(STANDARD.decode(s.trim()).map_err(|_| "invalid signature encoding")?)
            .map_err(|_| "invalid signature text".into())
    };
    let key = PublicKey::decode(&decode(public_key)?).map_err(|_| "invalid updater public key")?;
    let signature = Signature::decode(&decode(signature)?).map_err(|_| "invalid updater signature")?;
    key.verify(bytes, &signature, true)
        .map_err(|_| "updater signature verification failed".into())
}

impl Manifest {
    pub fn authenticated(bytes: &[u8], signature: &str, public_key: &str) -> Result<Self, String> {
        verify_signature(bytes, signature, public_key)?;
        serde_json::from_slice(bytes).map_err(|_| "invalid update manifest".into())
    }
    pub fn validate(&self, channel: Channel, tag: &str, source: &str, target: &str) -> Result<&Artifact, String> {
        let version = channel.version(tag)?;
        let (key, _) = target_spec(target)?;
        if self.schema_version != 1
            || self.repository != REPOSITORY
            || self.tag != tag
            || self.channel != channel
            || self.source_sha != source
            || self.target != target
            || self.version != version.to_string()
            || self.platforms.len() != 1
            || source.len() != 40
            || !source.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Err("update provenance mismatch".into());
        }
        let artifact = self.platforms.get(key).ok_or("update platform mismatch")?;
        if artifact.url != asset_url(tag, &artifact_name(&self.version, target)?)
            || artifact.size == 0
            || artifact.size > 4 * 1024 * 1024 * 1024
            || artifact.sha256.len() != 64
            || !artifact
                .sha256
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            || artifact.signature.trim().is_empty()
        {
            return Err("invalid update artifact".into());
        }
        Ok(artifact)
    }
}
impl Artifact {
    pub fn verify(&self, bytes: &[u8], public_key: &str) -> Result<(), String> {
        if bytes.len() as u64 != self.size || format!("{:x}", Sha256::digest(bytes)) != self.sha256 {
            return Err("update artifact integrity mismatch".into());
        }
        verify_signature(bytes, &self.signature, public_key)
    }
}

/// The consuming boundary: neither download errors nor unverified bytes may
/// reach the platform installer. Tests inject failures at this exact boundary.
pub fn install_verified<T>(
    download: Result<Vec<u8>, String>,
    artifact: &Artifact,
    public_key: &str,
    install: impl FnOnce(&[u8]) -> Result<T, String>,
) -> Result<T, String> {
    let bytes = download?;
    artifact.verify(&bytes, public_key)?;
    install(&bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn channels_are_disjoint_and_versions_follow_semver() {
        assert!(Channel::Test.version("gh-v3.1.2rc10").unwrap() > Channel::Test.version("gh-v3.1.2rc9").unwrap());
        assert!(Channel::Stable.version("v3.1.2").unwrap() > Channel::Test.version("gh-v3.1.2rc99").unwrap());
        for tag in [
            "gh-v3.1.2rc01",
            "gh-v3.1.2rc",
            "gh-v3.1.2rc1+foo",
            "v3.1.2",
            "gh-v3.1.2",
        ] {
            assert!(Channel::Test.version(tag).is_err(), "{tag}");
        }
        for tag in ["gh-v3.1.2rc1", "v3.1.2rc1", "v3.1.2-rc.1", "v3.1.2+foo"] {
            assert!(Channel::Stable.version(tag).is_err(), "{tag}");
        }
    }
    #[test]
    fn unsigned_or_description_metadata_is_never_a_signature() {
        for signature in ["", "app-adhoc", "installer-unsigned", "U0lHTkFUVVJF"] {
            assert!(verify_signature(b"payload", signature, "").is_err());
        }
    }
}
