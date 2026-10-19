//! Byte-preserving SHA-256 I/O and formatting shared by desktop consumers.
//!
//! sha2 0.11 no longer implements `io::Write` for hashers or `LowerHex` for
//! digest output. Keep the file stream and persisted lowercase hex unchanged.
use sha2::{Digest, Sha256};
use std::io::{self, Read};

/// Append a stream to an existing hash without buffering the whole file.
/// Retry interrupted reads, like `io::copy`, but propagate all other I/O errors.
pub fn update_from_reader(hasher: &mut Sha256, mut reader: impl Read) -> io::Result<()> {
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(read) => hasher.update(&buffer[..read]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

/// Two lowercase hex characters per byte, including leading zeroes.
pub fn lower_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        result.push(HEX[usize::from(byte >> 4)] as char);
        result.push(HEX[usize::from(byte & 15)] as char);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standard_sha256_vectors_keep_their_canonical_hex() {
        for (input, expected) in [
            (
                b"".as_slice(),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ),
            (
                b"abc".as_slice(),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            ),
            (
                b"c4".as_slice(),
                "0012a3fa000c5dc26ee658c3c58e12cecd58d6455cec3d5621f0c787675b38aa",
            ),
        ] {
            let mut hasher = Sha256::new();
            update_from_reader(&mut hasher, input).unwrap();
            assert_eq!(lower_hex(&hasher.finalize()), expected);
            assert_eq!(lower_hex(&Sha256::digest(input)), expected);
        }
        assert_eq!(lower_hex(&[0, 1, 15, 16, 128, 255]), "00010f1080ff");
    }

    #[test]
    fn streaming_hash_preserves_chunk_boundaries_and_bounded_reads() {
        // Fixed values from Python hashlib/OpenSSL, not the helper under test.
        for (size, expected) in [
            (
                65535,
                "6e1bebca6a8229364a162a72ef064826c4cd7457bf54f190ef782bd9deff3e42",
            ),
            (
                65536,
                "bf718b6f653bebc184e1479f1935b8da974d701b893afcf49e701f3e2f9f9c5a",
            ),
            (
                65537,
                "008ffc88d3c96a9f307524eb361e47c5222a887fc45fa0c1fb8d429c5c23b430",
            ),
            (
                131073,
                "7e009ea4ef882e385b3c0bcbbfa8d009bb0a633bdd764415c09182ee0e75da73",
            ),
        ] {
            struct BoundedReader(usize);
            impl Read for BoundedReader {
                fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                    assert!(buf.len() <= 64 * 1024);
                    let count = self.0.min(buf.len());
                    buf[..count].fill(b'a');
                    self.0 -= count;
                    Ok(count)
                }
            }
            let mut hasher = Sha256::new();
            update_from_reader(&mut hasher, BoundedReader(size)).unwrap();
            assert_eq!(lower_hex(&hasher.finalize()), expected);
        }
    }

    #[test]
    fn short_and_interrupted_reads_append_without_resetting_the_hash() {
        struct ShortReader(usize);
        impl Read for ShortReader {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                self.0 += 1;
                match self.0 {
                    1 | 3 => Err(io::ErrorKind::Interrupted.into()),
                    2 => {
                        buf[0] = b'a';
                        Ok(1)
                    }
                    4 => {
                        buf[0] = b'b';
                        Ok(1)
                    }
                    5 => {
                        buf[0] = b'c';
                        Ok(1)
                    }
                    _ => Ok(0),
                }
            }
        }
        let mut hasher = Sha256::new();
        hasher.update(b"prefix");
        update_from_reader(&mut hasher, ShortReader(0)).unwrap();
        assert_eq!(
            lower_hex(&hasher.finalize()),
            "bb55a5ab9f6a401bfca2ff147197209c59a002a3994731d8f2d2f45692f41846"
        );
    }

    #[test]
    fn a_read_failure_after_partial_data_is_not_reported_as_success() {
        struct FailingReader(bool);
        impl Read for FailingReader {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                if self.0 {
                    return Err(io::Error::new(io::ErrorKind::PermissionDenied, "fixture read failure"));
                }
                self.0 = true;
                buf[0] = b'a';
                Ok(1)
            }
        }
        let error = update_from_reader(&mut Sha256::new(), FailingReader(false)).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(error.to_string(), "fixture read failure");
    }
}
