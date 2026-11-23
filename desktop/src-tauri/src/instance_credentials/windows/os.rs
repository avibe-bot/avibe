//! OS primitives for the large-jar adapter. No credential enumeration, logging, or UI.
use super::{unavailable, Result, CHUNK, MAX_SEALED};
use std::{ptr, slice};
use windows_sys::Win32::{
    Foundation::LocalFree,
    Security::{
        Credentials::{CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC},
        Cryptography::{CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB},
    },
};
use zeroize::Zeroize;

// The keyring Windows adapter always requests enterprise persistence. These entries must be local.
pub(super) fn write_chunk(target: &str, data: &[u8]) -> Result<()> {
    if data.is_empty() || data.len() > CHUNK {
        return Err(unavailable());
    }
    let mut name: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let cred = CREDENTIALW {
        Type: CRED_TYPE_GENERIC,
        TargetName: name.as_mut_ptr(),
        CredentialBlobSize: data.len() as u32,
        CredentialBlob: data.as_ptr().cast_mut(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        ..Default::default()
    };
    // SAFETY: pointers are valid for the synchronous call; target is NUL-terminated.
    if unsafe { CredWriteW(&cred, 0) } == 0 {
        Err(unavailable())
    } else {
        Ok(())
    }
}

pub(super) fn dpapi(protect: bool, input: &[u8], entropy: &[u8; 32]) -> Result<Vec<u8>> {
    let blob = |s: &[u8]| -> Result<CRYPT_INTEGER_BLOB> {
        Ok(CRYPT_INTEGER_BLOB {
            cbData: s.len().try_into().map_err(|_| unavailable())?,
            pbData: s.as_ptr().cast_mut(),
        })
    };
    let (input, entropy) = (blob(input)?, blob(entropy)?);
    let mut out = CRYPT_INTEGER_BLOB::default();
    // SAFETY: both slices remain live. No description, prompt, UI, or machine protection flag.
    let ok = unsafe {
        if protect {
            CryptProtectData(
                &input,
                ptr::null(),
                &entropy,
                ptr::null(),
                ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        } else {
            CryptUnprotectData(
                &input,
                ptr::null_mut(),
                &entropy,
                ptr::null(),
                ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        }
    };
    if ok == 0 || out.pbData.is_null() {
        return Err(unavailable());
    }
    // SAFETY: DPAPI owns a LocalAlloc allocation of cbData bytes on success.
    let bytes = unsafe { slice::from_raw_parts_mut(out.pbData, out.cbData as usize) };
    let result = if bytes.len() <= MAX_SEALED {
        Ok(bytes.to_vec())
    } else {
        Err(unavailable())
    };
    bytes.zeroize();
    unsafe { LocalFree(out.pbData.cast()) };
    result
}
