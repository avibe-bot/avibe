//! A system the direct path does not route on: nothing is asked or routed.
use crate::direct::{
    rendezvous::{Reply, Request},
    wiring::Material,
};
use tauri::{AppHandle, Webview};

pub fn ask(_: &Webview, _: Request, _: impl Fn() -> bool + Send + 'static, done: impl FnOnce(Reply) + Send + 'static) {
    done(Reply::Failed);
}
pub fn install(_: &AppHandle, _: String, _: String, _: Material) {}
pub fn answer(_: &AppHandle, _: String, _: Material) {}
pub fn arguments(_: &str, _: &Material) -> Option<String> {
    None
}
pub async fn detach(_: &AppHandle, _: String) {}
