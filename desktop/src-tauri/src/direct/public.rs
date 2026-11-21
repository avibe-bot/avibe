//! System routing for the already authenticated, exact-authority public leg.
//! Never pass WebView headers or credentials to the machine's proxy.
use super::connect::Authority;
use std::net::{IpAddr, SocketAddr};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{lookup_host, TcpStream},
};

#[derive(PartialEq, Eq)]
enum Policy {
    Direct,
    Http(String, u16),
    Automatic,
    // A proxy exists for at least one scheme. Environment-wide Windows
    // inspection does not select a proxy for a particular destination.
    #[cfg(any(windows, test))]
    Configured,
    Unsupported,
}

impl super::Client {
    /// Includes unknown settings: callers must leave the original route untouched.
    pub async fn system_proxy_is_automatic() -> bool {
        matches!(inspect(None).await, Policy::Automatic | Policy::Unsupported)
    }
    /// Call before installing a native route. Windows' PAC replaces settings for
    /// the entire environment, even if this particular origin has a bypass.
    pub async fn system_proxy_allows_route(host: &str) -> bool {
        let Some(h) = Authority::managed(host) else {
            return false;
        };
        if cfg!(windows) {
            matches!(inspect(None).await, Policy::Direct)
        } else {
            matches!(inspect(Some(&h)).await, Policy::Direct | Policy::Http(..))
        }
    }
}
async fn inspect(host: Option<&Authority>) -> Policy {
    let host = host.map(|a| a.host().to_owned());
    tokio::task::spawn_blocking(move || native(host.as_deref()).unwrap_or(Policy::Unsupported))
        .await
        .unwrap_or(Policy::Unsupported)
}
#[cfg(not(any(windows, target_os = "macos")))]
fn native(_: Option<&str>) -> Option<Policy> {
    None
}

fn allowed(addr: SocketAddr, proxy: bool, port: u16) -> bool {
    let ip = addr.ip().to_canonical();
    !ip.is_unspecified() && (!ip.is_loopback() || proxy && addr.port() != port)
}
async fn dial(host: &str, port: u16, proxy: bool, listener: u16) -> Option<TcpStream> {
    let answers = lookup_host((host, port)).await.ok()?;
    let mut dials = tokio::task::JoinSet::new();
    for addr in answers.filter(|a| allowed(*a, proxy, listener)).take(16) {
        dials.spawn(TcpStream::connect(addr));
    }
    while let Some(result) = dials.join_next().await {
        if let Ok(Ok(s)) = result {
            return Some(s);
        }
    }
    None
}
pub(super) async fn open(a: &Authority, listener: u16) -> Option<TcpStream> {
    open_via(inspect(Some(a)).await, a, listener).await
}
async fn open_via(policy: Policy, a: &Authority, listener: u16) -> Option<TcpStream> {
    match policy {
        Policy::Direct => dial(a.host(), 443, false, listener).await,
        Policy::Http(host, port) => {
            let mut s = dial(&host, port, true, listener).await?;
            s.write_all(&a.direct_head()).await.ok()?;
            // Consume just the HTTP head; do not lose any opaque bytes following it.
            // The caller's establishment deadline also bounds this handshake.
            let mut head = Vec::new();
            while head.len() < 8192 {
                head.push(s.read_u8().await.ok()?);
                if head.ends_with(b"\r\n\r\n") {
                    let mut words = head.split(|&b| b == b'\r').next()?.split(|&b| b == b' ');
                    let version = words.next()?;
                    let status = words.next()?;
                    return (matches!(version, b"HTTP/1.0" | b"HTTP/1.1")
                        && matches!(status, [b'2', b'0'..=b'9', b'0'..=b'9']))
                    .then_some(s);
                }
            }
            None
        }
        #[cfg(any(windows, test))]
        Policy::Configured => None,
        Policy::Automatic | Policy::Unsupported => None,
    }
}

#[cfg(target_os = "macos")]
fn native(host: Option<&str>) -> Option<Policy> {
    use objc2::{rc::Retained, runtime::AnyObject};
    use objc2_foundation::{NSArray, NSDictionary, NSNumber, NSString, NSURL};
    type Dict = NSDictionary<NSString, AnyObject>;
    #[link(name = "CFNetwork", kind = "framework")]
    extern "C" {
        fn CFNetworkCopySystemProxySettings() -> *mut Dict;
        fn CFNetworkCopyProxiesForURL(u: *const NSURL, d: *const Dict) -> *mut NSArray<Dict>;
        static kCFProxyTypeKey: *const NSString;
        static kCFProxyTypeNone: *const NSString;
        static kCFProxyTypeHTTP: *const NSString;
        static kCFProxyTypeHTTPS: *const NSString;
        static kCFProxyTypeAutoConfigurationURL: *const NSString;
        static kCFProxyTypeAutoConfigurationJavaScript: *const NSString;
        static kCFProxyHostNameKey: *const NSString;
        static kCFProxyPortNumberKey: *const NSString;
        static kCFNetworkProxiesProxyAutoConfigEnable: *const NSString;
        static kCFNetworkProxiesProxyAutoDiscoveryEnable: *const NSString;
    }
    // CF containers, URL, strings and numbers are toll-free bridged. Copy
    // functions transfer +1 ownership. No script evaluation or credential API.
    unsafe {
        let d = Retained::from_raw(CFNetworkCopySystemProxySettings())?;
        for key in [
            kCFNetworkProxiesProxyAutoConfigEnable,
            kCFNetworkProxiesProxyAutoDiscoveryEnable,
        ] {
            if let Some(flag) = d.objectForKey(&*key) {
                if flag.downcast::<NSNumber>().ok()?.boolValue() {
                    return Some(Policy::Automatic);
                }
            }
        }
        let Some(host) = host else { return Some(Policy::Direct) };
        let url = NSURL::URLWithString(&NSString::from_str(&format!("https://{host}/")))?;
        let list = Retained::from_raw(CFNetworkCopyProxiesForURL(&*url, &*d))?;
        if list.count() == 0 {
            return None;
        }
        let proxy = list.objectAtIndex(0);
        let kind = proxy.objectForKey(&*kCFProxyTypeKey)?.downcast::<NSString>().ok()?;
        if *kind == *kCFProxyTypeNone {
            return Some(Policy::Direct);
        }
        if *kind == *kCFProxyTypeAutoConfigurationURL || *kind == *kCFProxyTypeAutoConfigurationJavaScript {
            return Some(Policy::Automatic);
        }
        if *kind != *kCFProxyTypeHTTP && *kind != *kCFProxyTypeHTTPS {
            return None;
        }
        let addr = proxy
            .objectForKey(&*kCFProxyHostNameKey)?
            .downcast::<NSString>()
            .ok()?
            .to_string();
        let port = u16::try_from(
            proxy
                .objectForKey(&*kCFProxyPortNumberKey)?
                .downcast::<NSNumber>()
                .ok()?
                .longLongValue(),
        )
        .ok()?;
        endpoint(&addr, port)
    }
}

fn endpoint(host: &str, port: u16) -> Option<Policy> {
    if port == 0
        || host.is_empty()
        || !(host.parse::<IpAddr>().is_ok()
            || host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_')))
    {
        return None;
    }
    Some(Policy::Http(host.to_owned(), port))
}

#[cfg(windows)]
fn native(host: Option<&str>) -> Option<Policy> {
    #[repr(C)]
    struct Config {
        auto: i32,
        pac: *mut u16,
        proxy: *mut u16,
        bypass: *mut u16,
    }
    #[link(name = "winhttp")]
    extern "system" {
        fn WinHttpGetIEProxyConfigForCurrentUser(c: *mut Config) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalFree(p: *mut std::ffi::c_void) -> *mut std::ffi::c_void;
    }
    impl Drop for Config {
        fn drop(&mut self) {
            for p in [self.pac, self.proxy, self.bypass] {
                if !p.is_null() {
                    unsafe {
                        GlobalFree(p.cast());
                    }
                }
            }
        }
    }
    // On success WinHTTP owns the null-terminated allocations until the caller
    // frees them with GlobalFree. Copy the strings before dropping the snapshot.
    unsafe fn string(p: *mut u16) -> Option<String> {
        if p.is_null() {
            return Some(String::new());
        }
        let n = (0..32768).find(|n| *p.add(*n) == 0)?;
        String::from_utf16(std::slice::from_raw_parts(p, n)).ok()
    }
    unsafe {
        let mut c: Config = std::mem::zeroed();
        if WinHttpGetIEProxyConfigForCurrentUser(&mut c) == 0 {
            return None;
        }
        windows_policy(
            host,
            c.auto != 0,
            &string(c.pac)?,
            &string(c.proxy)?,
            &string(c.bypass)?,
        )
    }
}
#[cfg(any(windows, test))]
fn windows_policy(host: Option<&str>, auto: bool, pac: &str, proxy: &str, bypass: &str) -> Option<Policy> {
    if auto || !pac.is_empty() {
        return Some(Policy::Automatic);
    }
    if proxy.trim().is_empty() {
        return Some(Policy::Direct);
    }
    // No host means environment-wide inspection: even a bypass or proxy for
    // a different scheme needs the system configuration left in place.
    let Some(host) = host else {
        return Some(Policy::Configured);
    };
    for rule in bypass.split(';').map(str::trim).filter(|s| !s.is_empty()) {
        if rule.eq_ignore_ascii_case("<local>") {
            continue;
        } // managed origins are FQDNs
        let rule = rule.to_ascii_lowercase();
        if rule == "*"
            || rule == host
            || rule
                .strip_prefix("*.")
                .is_some_and(|suffix| host.strip_suffix(suffix).is_some_and(|s| s.ends_with('.')))
        {
            return Some(Policy::Direct);
        }
        if !rule
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
            && !rule.starts_with("*.")
        {
            return None;
        }
    }
    let mut chosen = None;
    for part in proxy
        .split(|c: char| c == ';' || c.is_ascii_whitespace())
        .filter(|s| !s.is_empty())
    {
        let v = if let Some((scheme, v)) = part.split_once('=') {
            if !scheme.eq_ignore_ascii_case("https") {
                if !["http", "ftp"].iter().any(|s| scheme.eq_ignore_ascii_case(s)) {
                    return None;
                }
                continue;
            }
            v
        } else {
            part
        };
        if chosen.replace(v).is_some() {
            return None;
        }
    }
    let Some(v) = chosen else { return Some(Policy::Direct) };
    let v = v.strip_prefix("http://").unwrap_or(v);
    let (name, port) = if let Some(rest) = v.strip_prefix('[') {
        let (ip, t) = rest.split_once(']')?;
        (
            ip,
            if t.is_empty() {
                80
            } else {
                t.strip_prefix(':')?.parse().ok()?
            },
        )
    } else if let Some((h, p)) = v.split_once(':') {
        (h, p.parse().ok()?)
    } else {
        (v, 80)
    };
    endpoint(name, port)
}

#[cfg(test)]
#[path = "../../tests/direct/public_fallback.rs"]
mod tests;
