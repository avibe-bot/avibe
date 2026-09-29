use avibe_runtime_host::download::{fetch, Length};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const PAYLOAD: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
const EXACT: Length = Length::Exact(PAYLOAD.len() as u64);

/// One scripted response per connection. Once the script is exhausted the
/// listener closes and the source refuses connections.
#[derive(Clone, Copy)]
enum Reply {
    /// The file, honouring `Range`.
    Serve,
    /// The whole file with 200, whatever the request asked for.
    IgnoreRange,
    Status(u16),
    /// Promises the rest of the file, sends this many bytes, and drops the connection.
    Cut(usize),
    /// Sends this many bytes, then stops sending without closing.
    Stall(usize),
    /// No length announced: sends this many bytes and closes cleanly.
    Unsized(usize),
    /// 206 from the wrong offset.
    WrongOffset,
    /// The file followed by bytes that cannot belong to it.
    Oversized,
}

struct Source {
    url: String,
    /// The `Range` header of each request received.
    ranges: Arc<Mutex<Vec<Option<String>>>>,
}

impl Source {
    fn ranges(&self) -> Vec<Option<String>> {
        self.ranges.lock().unwrap().clone()
    }
}

fn source(replies: impl IntoIterator<Item = Reply>) -> Source {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/asset", listener.local_addr().unwrap());
    let ranges = Arc::new(Mutex::new(Vec::new()));
    let seen = ranges.clone();
    let replies: Vec<Reply> = replies.into_iter().collect();
    std::thread::spawn(move || {
        for reply in replies {
            let (stream, _) = listener.accept().unwrap();
            let seen = seen.clone();
            // Each connection runs alone, so a stalled reply never delays the next.
            std::thread::spawn(move || respond(stream, reply, &seen));
        }
    });
    Source { url, ranges }
}

fn respond(mut stream: TcpStream, reply: Reply, seen: &Mutex<Vec<Option<String>>>) {
    let mut request = Vec::new();
    let mut buffer = [0; 1024];
    while !request.windows(4).any(|window| window == b"\r\n\r\n") {
        let read = stream.read(&mut buffer).unwrap();
        assert!(read > 0, "request ended before its headers");
        request.extend_from_slice(&buffer[..read]);
    }
    let range = String::from_utf8(request).unwrap().lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("range").then(|| value.trim().to_owned())
    });
    seen.lock().unwrap().push(range.clone());
    let offset = range
        .as_deref()
        .and_then(|range| range.strip_prefix("bytes=")?.strip_suffix('-')?.parse::<usize>().ok())
        .unwrap_or(0);
    let rest = &PAYLOAD[offset..];
    let partial = |body: &[u8], length: usize| {
        let mut head = if offset == 0 {
            "HTTP/1.1 200 OK\r\n".to_owned()
        } else {
            format!(
                "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {offset}-{}/{}\r\n",
                PAYLOAD.len() - 1,
                PAYLOAD.len()
            )
        };
        head.push_str(&format!("Content-Length: {length}\r\nConnection: close\r\n\r\n"));
        [head.as_bytes(), body].concat()
    };
    let response = match reply {
        Reply::Serve => partial(rest, rest.len()),
        Reply::IgnoreRange => [
            format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", PAYLOAD.len()).as_bytes(),
            PAYLOAD,
        ]
        .concat(),
        Reply::Status(code) => format!("HTTP/1.1 {code} Nope\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").into_bytes(),
        Reply::Cut(sent) | Reply::Stall(sent) => partial(&rest[..sent], rest.len()),
        Reply::Unsized(sent) => [b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n".as_slice(), &PAYLOAD[..sent]].concat(),
        Reply::WrongOffset => format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-{}/{}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            PAYLOAD.len() - 1,
            PAYLOAD.len(),
            PAYLOAD.len()
        )
        .into_bytes(),
        Reply::Oversized => {
            let body = [PAYLOAD, b"extra"].concat();
            partial(&body, body.len())
        }
    };
    // A source whose peer already gave up may fail to write; that is the point.
    let _ = stream.write_all(&response);
    let _ = stream.flush();
    if matches!(reply, Reply::Stall(_)) {
        std::thread::sleep(Duration::from_secs(3));
    }
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(2))
        .read_timeout(Duration::from_millis(300))
        .build()
        .unwrap()
}

async fn download(sources: &[&Source], start: usize, length: Length) -> Result<(Vec<u8>, usize), String> {
    let urls: Vec<String> = sources.iter().map(|source| source.url.clone()).collect();
    fetch(&client(), &urls, start, length).await
}

#[tokio::test]
async fn a_mirror_miss_falls_back_to_github_from_the_start() {
    let mirror = source([Reply::Status(404)]);
    let github = source([Reply::Serve]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
    assert_eq!(mirror.ranges(), [None]);
    assert_eq!(github.ranges(), [None]);
}

#[tokio::test]
async fn a_stalled_or_dropped_source_hands_its_bytes_to_the_next_one() {
    for (failure, sent) in [(Reply::Stall(10), 10), (Reply::Cut(7), 7)] {
        let mirror = source([failure]);
        let github = source([Reply::Serve]);
        assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
        assert_eq!(github.ranges(), [Some(format!("bytes={sent}-"))]);
    }
}

#[tokio::test]
async fn resumption_alternates_sources_until_each_had_two_attempts() {
    let mirror = source([Reply::Cut(5), Reply::Serve]);
    let github = source([Reply::Cut(9)]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 0)));
    assert_eq!(mirror.ranges(), [None, Some("bytes=14-".into())]);
    assert_eq!(github.ranges(), [Some("bytes=5-".into())]);

    let mirror = source([Reply::Status(503), Reply::Status(503), Reply::Serve]);
    let github = source([Reply::Status(502), Reply::Status(502)]);
    assert_eq!(
        download(&[&mirror, &github], 0, EXACT).await,
        Err("download failed: HTTP 502 Bad Gateway".into())
    );
    assert_eq!(mirror.ranges().len(), 2);
    assert_eq!(github.ranges().len(), 2);
}

#[tokio::test]
async fn the_last_source_that_worked_is_tried_first() {
    let mirror = source([]);
    let github = source([Reply::Serve]);
    assert_eq!(download(&[&mirror, &github], 1, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
    assert!(mirror.ranges().is_empty());
}

#[tokio::test]
async fn a_source_that_ignores_or_misplaces_the_range_never_splices_bytes() {
    let mirror = source([Reply::Cut(12)]);
    let github = source([Reply::IgnoreRange]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
    assert_eq!(github.ranges(), [Some("bytes=12-".into())]);

    let mirror = source([Reply::Cut(12), Reply::Serve]);
    let github = source([Reply::WrongOffset]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 0)));
    assert_eq!(mirror.ranges(), [None, Some("bytes=12-".into())]);
}

#[tokio::test]
async fn a_clean_early_end_is_a_failure_only_when_the_length_is_pinned() {
    let mirror = source([Reply::Unsized(20)]);
    let github = source([Reply::Serve]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
    assert_eq!(github.ranges(), [Some("bytes=20-".into())]);

    let only = source([Reply::Unsized(20)]);
    assert_eq!(
        download(&[&only], 0, Length::AtMost(1024)).await,
        Ok((PAYLOAD[..20].to_vec(), 0))
    );
}

#[tokio::test]
async fn bytes_beyond_the_expected_size_are_discarded_with_their_source() {
    let mirror = source([Reply::Oversized]);
    let github = source([Reply::Serve]);
    assert_eq!(download(&[&mirror, &github], 0, EXACT).await, Ok((PAYLOAD.to_vec(), 1)));
    assert_eq!(github.ranges(), [None], "nothing from the oversized source is kept");

    let only = source([Reply::Oversized, Reply::Oversized]);
    assert_eq!(
        download(&[&only], 0, Length::AtMost(PAYLOAD.len() as u64)).await,
        Err("download exceeds its expected size".into())
    );
}
