//! Release gate: authenticate exactly the same manifest and bytes as the shell.
use avibe_runtime_host::update::{manifest_name, Channel, Manifest};
use std::{env, fs, path::Path};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.len() != 5 {
        return Err("usage: verify_update DIRECTORY TAG SOURCE_SHA TARGET PUBLIC_KEY_FILE".into());
    }
    let root = Path::new(&args[0]);
    let name = manifest_name(&args[3]);
    let key = fs::read_to_string(&args[4])?;
    let manifest = Manifest::authenticated(
        &fs::read(root.join(&name))?,
        &fs::read_to_string(root.join(format!("{name}.sig")))?,
        &key,
    )?;
    let channel = if args[1].starts_with("gh-v") {
        Channel::Test
    } else {
        Channel::Stable
    };
    let artifact = manifest.validate(channel, &args[1], &args[2], &args[3])?;
    artifact.verify(
        &fs::read(root.join(artifact.url.rsplit('/').next().ok_or("invalid URL")?))?,
        &key,
    )?;
    println!(
        "Authenticated {} {} {}",
        manifest.tag, manifest.target, manifest.source_sha
    );
    Ok(())
}
