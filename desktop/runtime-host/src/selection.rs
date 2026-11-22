//! What a user explicitly selected in another app (macOS Services, "Ask
//! Vibey"), with the bounds and file-identity rules that apply before any of it
//! reaches a Workbench. Pure data and filesystem checks; no OS integration.
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};

/// Selected text, in bytes.
pub const TEXT_LIMIT: u64 = 1024 * 1024;
/// One selected URL, in bytes.
pub const URL_LIMIT: u64 = 32 * 1024;
/// Selected files in one share.
pub const FILE_COUNT_LIMIT: usize = 10;

/// One share as Services delivered it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Input {
    Text(String),
    Url(String),
    Files(Vec<PathBuf>),
    /// The selection could not be read within the bounds above.
    Unreadable,
}

/// Opens an ordinary file without following a final symlink, refusing one
/// that changed identity while it was being opened.
pub fn ordinary_open(path: &Path) -> io::Result<File> {
    let refused = |what: &str| io::Error::new(io::ErrorKind::InvalidInput, what.to_owned());
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.file_type().is_symlink() {
        return Err(refused("not an ordinary file"));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if !after.is_file() || after.file_type().is_symlink() || !unchanged(&before, &opened) || !unchanged(&opened, &after)
    {
        return Err(refused("changed while opened"));
    }
    Ok(file)
}

/// Same ordinary file with the same contents marker: device, inode, change
/// time, length and modification time.
pub fn unchanged(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if before.dev() != after.dev()
            || before.ino() != after.ino()
            || before.ctime() != after.ctime()
            || before.ctime_nsec() != after.ctime_nsec()
        {
            return false;
        }
    }
    before.is_file()
        && after.is_file()
        && before.len() == after.len()
        && before.modified().ok() == after.modified().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_ordinary_file_opens_and_matches_itself() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("note.txt");
        fs::write(&path, b"selected").unwrap();
        let file = ordinary_open(&path).unwrap();
        assert!(unchanged(&fs::metadata(&path).unwrap(), &file.metadata().unwrap()));
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_a_directory_and_a_missing_path_are_refused() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("target.txt");
        fs::write(&target, b"secret").unwrap();
        let link = directory.path().join("link.txt");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(ordinary_open(&link).is_err());
        assert!(ordinary_open(directory.path()).is_err());
        assert!(ordinary_open(&directory.path().join("gone.txt")).is_err());
    }

    #[test]
    fn a_rewritten_file_is_not_unchanged() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("note.txt");
        fs::write(&path, b"selected").unwrap();
        let selected = fs::metadata(&path).unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(20)
            .unwrap();
        assert!(!unchanged(&selected, &fs::metadata(&path).unwrap()));
    }

    // Identity (device and inode) is a Unix notion; Services runs on macOS.
    #[cfg(unix)]
    #[test]
    fn a_replaced_file_of_the_same_size_is_not_unchanged() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("note.txt");
        fs::write(&path, vec![1; 20]).unwrap();
        let same_size = fs::metadata(&path).unwrap();
        fs::write(directory.path().join("other.txt"), vec![0; 20]).unwrap();
        fs::rename(directory.path().join("other.txt"), &path).unwrap();
        assert!(!unchanged(&same_size, &fs::metadata(&path).unwrap()));
    }
}
