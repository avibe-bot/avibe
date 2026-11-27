//! A system the direct path does not route on: nothing is asked or routed.
use crate::direct::{
    rendezvous::{Reply, Request},
    wiring::Material,
    WindowRegistration,
};
use tauri::{AppHandle, Webview};

pub fn ask(_: &Webview, _: Request, _: impl Fn() -> bool + Send + 'static, done: impl FnOnce(Reply) + Send + 'static) {
    done(Reply::Failed);
}
pub fn install(
    _: &AppHandle,
    _: String,
    _: String,
    _: Material,
    _: WindowRegistration,
    _: impl Fn() -> bool + Send + 'static,
    _: impl FnOnce(bool) + Send + 'static,
) {
}
pub fn answer(_: &AppHandle, _: String, _: Material, _: WindowRegistration, _: impl Fn() -> bool + Send + 'static) {}
pub fn block(_: &AppHandle, _: String, _: String, _: std::net::SocketAddrV4, _: impl Fn() -> bool + Send + 'static) {}
pub fn forget_closed(_: impl Fn(&str) -> bool) {}
pub fn arguments(_: &str, _: &Material) -> Option<String> {
    None
}
pub async fn detach(_: &AppHandle, _: String) {}
