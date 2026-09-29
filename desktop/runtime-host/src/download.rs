//! Downloads one immutable file from ordered, byte-identical sources.
//!
//! The client owns the connect timeout and the stall watchdog (its read
//! timeout); this module owns the source order and resumption. Callers verify
//! the returned bytes: a source is trusted for availability, never integrity.
use reqwest::{
    header::{CONTENT_RANGE, RANGE},
    StatusCode,
};

/// How many bytes a complete download holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Length {
    /// Exactly this many bytes, as pinned by signed metadata.
    Exact(u64),
    /// Unknown in advance, but never more than this many bytes.
    AtMost(u64),
}

enum Failure {
    /// The source can still serve the rest of the file later or elsewhere.
    Retry(String),
    /// The source served bytes that cannot belong to the file.
    Discard(String),
}

/// Fetches from `sources`, starting at index `start` and trying each source at
/// most twice in rotation. A failed or stalled source hands the bytes already
/// received to the next one, which resumes with `Range`; a source that
/// ignores the range restarts from zero. Returns the bytes and the index of
/// the source that completed them.
pub async fn fetch(
    client: &reqwest::Client,
    sources: &[String],
    start: usize,
    length: Length,
) -> Result<(Vec<u8>, usize), String> {
    if sources.is_empty() {
        return Err("no download source".into());
    }
    let mut data = Vec::new();
    let mut last = String::new();
    for attempt in 0..sources.len() * 2 {
        let index = (start + attempt) % sources.len();
        match continue_from(client, &sources[index], &mut data, length).await {
            Ok(()) => return Ok((data, index)),
            Err(Failure::Retry(error)) => last = error,
            Err(Failure::Discard(error)) => {
                data.clear();
                last = error;
            }
        }
    }
    Err(last)
}

async fn continue_from(client: &reqwest::Client, url: &str, data: &mut Vec<u8>, length: Length) -> Result<(), Failure> {
    let retry = |error: reqwest::Error| Failure::Retry(format!("download failed: {error}"));
    let mut request = client.get(url);
    if !data.is_empty() {
        request = request.header(RANGE, format!("bytes={}-", data.len()));
    }
    let mut response = request.send().await.map_err(retry)?;
    match response.status() {
        StatusCode::OK => data.clear(),
        StatusCode::PARTIAL_CONTENT if !data.is_empty() => {
            let resumes_here = response
                .headers()
                .get(CONTENT_RANGE)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.strip_prefix("bytes "))
                .and_then(|value| value.split_once('-'))
                .and_then(|(first, _)| first.parse::<usize>().ok())
                == Some(data.len());
            if !resumes_here {
                return Err(Failure::Retry("download resumed at the wrong offset".into()));
            }
        }
        status => return Err(Failure::Retry(format!("download failed: HTTP {status}"))),
    }
    let limit = match length {
        Length::Exact(size) | Length::AtMost(size) => size,
    };
    while let Some(chunk) = response.chunk().await.map_err(retry)? {
        if (data.len() + chunk.len()) as u64 > limit {
            return Err(Failure::Discard("download exceeds its expected size".into()));
        }
        data.extend_from_slice(&chunk);
    }
    if matches!(length, Length::Exact(size) if data.len() as u64 != size) {
        return Err(Failure::Retry("download ended early".into()));
    }
    Ok(())
}
