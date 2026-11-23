//! The proxy recognizes one configured managed origin. It never forwards the
//! browser's proxy headers. Bytes after the head still belong to the browser.
const HEAD_LIMIT: usize = 4096;

#[derive(Clone, PartialEq, Eq)]
pub struct Authority(String);

impl Authority {
    pub fn managed(host: &str) -> Option<Self> {
        let label = host.strip_suffix("-app.avibe.bot")?;
        if label.is_empty()
            || label.len() + 4 > 63
            || label.starts_with('-')
            || label.ends_with('-')
            || !label
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        {
            return None;
        }
        Some(Self(host.to_owned()))
    }

    pub fn host(&self) -> &str {
        &self.0
    }

    pub fn direct_head(&self) -> Vec<u8> {
        format!("CONNECT {0}:443 HTTP/1.1\r\nHost: {0}:443\r\n\r\n", self.host()).into_bytes()
    }
}

#[derive(PartialEq, Eq, Debug)]
pub enum Head<T = ()> {
    More,
    Refuse(u16),
    Accept(usize, T),
}

pub fn parse_connect<T>(
    input: &[u8],
    authority: &Authority,
    authenticate: impl FnOnce(Option<&str>) -> Option<T>,
) -> Head<T> {
    let end = input[..input.len().min(HEAD_LIMIT + 4)]
        .windows(4)
        .position(|s| s == b"\r\n\r\n");
    let Some(end) = end else {
        return if input.len() >= HEAD_LIMIT + 4 {
            Head::Refuse(400)
        } else {
            Head::More
        };
    };
    let Ok(text) = std::str::from_utf8(&input[..end]) else {
        return Head::Refuse(400);
    };
    let mut lines = text.split("\r\n");
    let mut first = lines.next().unwrap_or("").split(' ');
    if first.next() != Some("CONNECT") {
        return Head::Refuse(405);
    }
    let target = first.next().unwrap_or("");
    if first.next() != Some("HTTP/1.1") || first.next().is_some() {
        return Head::Refuse(400);
    }
    let Some(host) = target.strip_suffix(":443") else {
        return Head::Refuse(403);
    };
    // WKWebsiteDataStore's matchDomains also sends child hosts here.
    // Managed instance origins are single labels under avibe.bot; their
    // children are not served by Avibe or covered by its wildcard cert.
    // Refuse locally rather than ever forwarding one to the instance.
    if !host.eq_ignore_ascii_case(authority.host()) {
        return Head::Refuse(403);
    }
    let mut saw_host = false;
    let mut proxy_auth = None;
    for line in lines {
        let Some((key, value)) = line.split_once(':') else {
            return Head::Refuse(400);
        };
        if key.is_empty()
            || !key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
            || !value.bytes().all(|b| b == b'\t' || (32..=126).contains(&b))
            || key.eq_ignore_ascii_case("Content-Length")
            || key.eq_ignore_ascii_case("Transfer-Encoding")
        {
            return Head::Refuse(400);
        }
        if key.eq_ignore_ascii_case("Proxy-Authorization")
            && proxy_auth.replace(value.trim_matches([' ', '\t'])).is_some()
        {
            return Head::Refuse(400);
        }
        if key.eq_ignore_ascii_case("Host") {
            let v = value.trim_matches([' ', '\t']);
            if saw_host || !(v.eq_ignore_ascii_case(host) || v.eq_ignore_ascii_case(target)) {
                return Head::Refuse(400);
            }
            saw_host = true;
        }
    }
    match authenticate(proxy_auth) {
        Some(grant) => Head::Accept(end + 4, grant),
        None => Head::Refuse(407),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse_connect(input: &[u8], a: &Authority) -> Head {
        super::parse_connect(input, a, |_| Some(()))
    }
    #[test]
    fn protected_listener_requires_the_current_proxy_credential() {
        let a = Authority::managed("desk-app.avibe.bot").unwrap();
        let check = |headers: &str| {
            super::parse_connect(
                format!("CONNECT desk-app.avibe.bot:443 HTTP/1.1\r\n{headers}\r\n").as_bytes(),
                &a,
                |value| (value == Some("Basic fixture-current")).then_some(()),
            )
        };
        assert_eq!(check(""), Head::Refuse(407));
        assert_eq!(check("Authorization: Basic fixture-current\r\n"), Head::Refuse(407));
        assert_eq!(check("Proxy-Authorization: Basic fixture-old\r\n"), Head::Refuse(407));
        assert_eq!(
            check("Proxy-Authorization: Basic fixture-current\r\nProxy-Authorization: Basic fixture-current\r\n"),
            Head::Refuse(400)
        );
        assert!(matches!(
            check("Proxy-Authorization: Basic fixture-current\r\n"),
            Head::Accept(_, ())
        ));
        assert!(!a.direct_head().windows(5).any(|s| s == b"Basic"));
    }
    #[test]
    fn exact_authority_and_unconsumed_client_hello() {
        let a = Authority::managed("desk-app.avibe.bot").unwrap();
        let mut head = a.direct_head();
        for n in 0..head.len() {
            assert_eq!(parse_connect(&head[..n], &a), Head::More);
        }
        let n = head.len();
        head.extend_from_slice(&[0x16, 3, 1, 0xff]);
        assert_eq!(parse_connect(&head, &a), Head::Accept(n, ()));
        for t in [
            "child.desk-app.avibe.bot:443",
            "desk-app.avibe.bot:80",
            "desk-app.avibe.bot",
            "desk-app.avibe.bot.:443",
            "127.0.0.1:443",
        ] {
            assert_eq!(
                parse_connect(format!("CONNECT {t} HTTP/1.1\r\n\r\n").as_bytes(), &a),
                Head::Refuse(403)
            );
        }
        for bad in [
            "Host: elsewhere",
            "Host: desk-app.avibe.bot\r\nHost: desk-app.avibe.bot",
            "Content-Length: 0",
            "Transfer-Encoding: chunked",
            "Host : desk-app.avibe.bot",
            "X-Bad: a\rb",
        ] {
            assert_eq!(
                parse_connect(
                    format!("CONNECT desk-app.avibe.bot:443 HTTP/1.1\r\n{bad}\r\n\r\n").as_bytes(),
                    &a
                ),
                Head::Refuse(400)
            );
        }
        assert_eq!(parse_connect(&vec![b'A'; HEAD_LIMIT + 4], &a), Head::Refuse(400));
    }
}
