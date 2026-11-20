//! AppKit and WebKit for sharing a Show from the desktop app (C1 v1 outbound,
//! docs/plans/2026-11-19-desktop-show-outbound-sharing.md): run the share
//! scripts in an isolated content world, capture a region of the web view,
//! encode it, and present the system share menu.
//!
//! Nothing here decides who may share what. `show_share` owns the request,
//! the window binding and the operation; this module only does what it is
//! told, on the main thread, and reports back through callbacks. It names no
//! crate item so a probe can compile it from its source file.

use std::{cell::RefCell, path::Path};

use block2::RcBlock;
use objc2::{
    define_class,
    rc::Retained,
    runtime::{AnyObject, NSObject, NSObjectProtocol, ProtocolObject},
    AnyThread, DefinedClass, MainThreadMarker, MainThreadOnly, Message,
};
use objc2_app_kit::{
    NSBitmapImageFileType, NSBitmapImageRep, NSImage, NSImageView, NSSharingService, NSSharingServiceDelegate,
    NSSharingServicePicker, NSSharingServicePickerDelegate, NSView,
};
use objc2_foundation::{
    NSArray, NSCocoaErrorDomain, NSDictionary, NSError, NSPoint, NSRect, NSRectEdge, NSString, NSUserCancelledError,
    NSURL,
};
use objc2_web_kit::{WKContentWorld, WKSnapshotConfiguration, WKWebView};

/// This Mac's major and minor version.
pub fn os_version() -> (u64, u64) {
    let version = objc2_foundation::NSProcessInfo::processInfo().operatingSystemVersion();
    (version.majorVersion as u64, version.minorVersion as u64)
}

/// The largest capture in device pixels, as on the iPhone.
const MAX_PIXELS: f64 = 40_000_000.0;

/// Whether a capture of the whole web view stays under the pixel cap.
pub fn within_pixel_cap(webview: &WKWebView) -> bool {
    let (bounds, _, scale) = geometry(webview);
    bounds.size.width * bounds.size.height * scale * scale <= MAX_PIXELS
}

/// The URL of the web view's current top document.
pub fn document_url(webview: &WKWebView) -> Option<String> {
    // SAFETY: Main thread; reading the web view's current URL.
    unsafe { webview.URL() }
        .and_then(|url| url.absoluteString())
        .map(|url| url.to_string())
}

/// The web view's bounds, whether it is flipped, and its window's backing scale.
pub fn geometry(webview: &WKWebView) -> (NSRect, bool, f64) {
    let view: &NSView = webview;
    let scale = view.window().map(|window| window.backingScaleFactor()).unwrap_or(2.0);
    (view.bounds(), view.isFlipped(), scale)
}

/// Runs `body` as an async function in WebKit's default client world, a world
/// the page's own scripts cannot reach, with string `arguments`. The script
/// returns a JSON string; `done` receives it, or `None` on any error.
pub fn call_script(
    webview: &WKWebView,
    mtm: MainThreadMarker,
    body: &str,
    arguments: &[(&str, &str)],
    done: impl FnOnce(Option<String>) + 'static,
) {
    let keys: Vec<Retained<NSString>> = arguments.iter().map(|(key, _)| NSString::from_str(key)).collect();
    let values: Vec<Retained<AnyObject>> = arguments
        .iter()
        .map(|(_, value)| Retained::into_super(Retained::into_super(NSString::from_str(value))))
        .collect();
    let key_refs: Vec<&NSString> = keys.iter().map(|key| &**key).collect();
    let dictionary = NSDictionary::from_retained_objects(&key_refs, &values);
    let done = RefCell::new(Some(done));
    let block = RcBlock::new(move |result: *mut AnyObject, error: *mut NSError| {
        let Some(done) = done.borrow_mut().take() else { return };
        if !error.is_null() || result.is_null() {
            done(None);
            return;
        }
        // SAFETY: WebKit passes a live object for the duration of the call.
        let object = unsafe { &*result };
        done(object.downcast_ref::<NSString>().map(|value| value.to_string()));
    });
    // SAFETY: Main thread; every argument is a plain NSString and the world is
    // WebKit's own isolated client world.
    unsafe {
        let world = WKContentWorld::defaultClientWorld(mtm);
        webview.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler(
            &NSString::from_str(body),
            Some(&dictionary),
            None,
            &world,
            Some(&block),
        );
    }
}

/// Captures the whole web view, after its next screen update.
pub fn snapshot(webview: &WKWebView, mtm: MainThreadMarker, done: impl FnOnce(Option<Retained<NSImage>>) + 'static) {
    let done = RefCell::new(Some(done));
    let block = RcBlock::new(move |image: *mut NSImage, error: *mut NSError| {
        let Some(done) = done.borrow_mut().take() else { return };
        if !error.is_null() || image.is_null() {
            done(None);
            return;
        }
        // SAFETY: WebKit passes a live image; retaining keeps it past the call.
        done(unsafe { Retained::retain(image) });
    });
    // SAFETY: Main thread; the configuration is fully initialised here.
    unsafe {
        let configuration = WKSnapshotConfiguration::new(mtm);
        configuration.setAfterScreenUpdates(true);
        webview.takeSnapshotWithConfiguration_completionHandler(Some(&configuration), &block);
    }
}

/// The capture as PNG, at the pixels WebKit rendered.
pub fn png(image: &NSImage) -> Option<Vec<u8>> {
    let tiff = image.TIFFRepresentation()?;
    let bitmap = NSBitmapImageRep::imageRepWithData(&tiff)?;
    // SAFETY: An empty property dictionary is a valid argument.
    let data = unsafe { bitmap.representationUsingType_properties(NSBitmapImageFileType::PNG, &NSDictionary::new()) }?;
    Some(data.to_vec())
}

/// The capture as a one-page PDF holding the same pixels. Printing the page
/// instead would reflow the Show, reveal hidden content and lose canvas state.
pub fn pdf(image: &NSImage, mtm: MainThreadMarker) -> Option<Vec<u8>> {
    let size = image.size();
    if size.width <= 0.0 || size.height <= 0.0 {
        return None;
    }
    let bounds = NSRect::new(NSPoint::new(0.0, 0.0), size);
    let view = NSImageView::initWithFrame(NSImageView::alloc(mtm), bounds);
    view.setImage(Some(image));
    let data = view.dataWithPDFInsideRect(bounds);
    let bytes = data.to_vec();
    (!bytes.is_empty()).then_some(bytes)
}

/// What the person did with the share menu.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// A service reported that it took the item.
    Completed,
    /// The menu closed without a service, or the service was cancelled.
    Cancelled,
    /// The service reported an error.
    Failed,
}

/// One item for the share menu: a web link or a file in the share directory.
pub enum Item<'a> {
    Link(&'a str),
    File(&'a Path),
}

/// Runs once with the outcome of a share menu.
pub type Report = Box<dyn FnOnce(Outcome)>;

pub struct DelegateFields {
    report: RefCell<Option<Report>>,
}

define_class!(
    /// Receives the share menu's and the chosen service's callbacks, and
    /// reports the first outcome once.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeShowShareDelegate"]
    #[ivars = DelegateFields]
    pub struct ShareDelegate;

    unsafe impl NSObjectProtocol for ShareDelegate {}

    unsafe impl NSSharingServicePickerDelegate for ShareDelegate {
        #[unsafe(method_id(sharingServicePicker:delegateForSharingService:))]
        fn delegate_for_service(
            &self,
            _picker: &NSSharingServicePicker,
            _service: &NSSharingService,
        ) -> Option<Retained<ProtocolObject<dyn NSSharingServiceDelegate>>> {
            Some(ProtocolObject::from_retained(self.retain()))
        }

        #[unsafe(method(sharingServicePicker:didChooseSharingService:))]
        fn did_choose(&self, _picker: &NSSharingServicePicker, service: Option<&NSSharingService>) {
            if service.is_none() {
                self.report(Outcome::Cancelled);
            }
        }
    }

    unsafe impl NSSharingServiceDelegate for ShareDelegate {
        #[unsafe(method(sharingService:didShareItems:))]
        fn did_share(&self, _service: &NSSharingService, _items: &NSArray) {
            self.report(Outcome::Completed);
        }

        #[unsafe(method(sharingService:didFailToShareItems:error:))]
        fn did_fail(&self, _service: &NSSharingService, _items: &NSArray, error: &NSError) {
            // SAFETY: Reading a constant the framework defines.
            let cancelled =
                error.code() == NSUserCancelledError && error.domain().isEqualToString(unsafe { NSCocoaErrorDomain });
            self.report(if cancelled { Outcome::Cancelled } else { Outcome::Failed });
        }
    }
);

impl ShareDelegate {
    fn new(mtm: MainThreadMarker, report: Report) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(DelegateFields {
            report: RefCell::new(Some(report)),
        });
        // SAFETY: NSObject's designated initialiser.
        unsafe { objc2::msg_send![super(this), init] }
    }

    fn report(&self, outcome: Outcome) {
        let report = self.ivars().report.borrow_mut().take();
        if let Some(report) = report {
            report(outcome);
        }
    }
}

/// An open share menu. Dropping it does not close the menu; `close` does.
pub struct Picker {
    picker: Retained<NSSharingServicePicker>,
    _delegate: Retained<ShareDelegate>,
}

impl Picker {
    /// Closes the menu if it is still open. Its delegate then reports a
    /// cancellation, which the operation has already moved past.
    pub fn close(&self) {
        self.picker.close();
    }
}

/// Shows the system share menu for `item`, beside `anchor` in the web view.
/// `report` runs once, on the main thread, with the outcome.
pub fn present(
    webview: &WKWebView,
    mtm: MainThreadMarker,
    item: Item<'_>,
    anchor: NSRect,
    report: Report,
) -> Option<Picker> {
    let object: Retained<AnyObject> = match item {
        Item::Link(link) => {
            let url = NSURL::URLWithString(&NSString::from_str(link))?;
            Retained::into_super(Retained::into_super(url))
        }
        Item::File(path) => {
            let url = NSURL::fileURLWithPath(&NSString::from_str(path.to_str()?));
            Retained::into_super(Retained::into_super(url))
        }
    };
    let items = NSArray::from_retained_slice(&[object]);
    let delegate = ShareDelegate::new(mtm, report);
    // SAFETY: Every item is an NSURL, which the picker accepts.
    let picker = unsafe { NSSharingServicePicker::initWithItems(NSSharingServicePicker::alloc(), &items) };
    picker.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    let view: &NSView = webview;
    view.window()?;
    picker.showRelativeToRect_ofView_preferredEdge(anchor, view, NSRectEdge::MinY);
    Some(Picker {
        picker,
        _delegate: delegate,
    })
}

/// Reads a Show through this instance's own read APIs, in a world the page
/// cannot reach: whether the user may use it, and its link, as the iPhone App
/// reads them (ios/Avibe/ShowShareCoordinator.swift). A Show the user may use
/// but that has no link (a limited Show while Avibe Cloud is not connected) is
/// still allowed, with an empty `url`: a link share then fails, an export does
/// not need one. It reads no DOM: which Show, and which pixels, are the
/// shell's to decide. `visible` only says the page is on screen now.
pub const READ: &str = r#"
if (location.origin !== origin) return {allowed:false};
const read = async (path) => {
  const response = await fetch(path, {credentials:'same-origin', cache:'no-store', redirect:'error'});
  if (!response.ok || new URL(response.url).origin !== origin) throw new Error('access');
  return response.json();
};
const path = '/api/show-pages/' + encodeURIComponent(sessionID);
const access = await read(path + '/access');
if (access.can_use !== true) return {allowed:false};
const page = await read(path);
if (page.session_id !== sessionID || page.offline === true
    || !['private','limited','public'].includes(page.visibility)) return {allowed:false};
const href = page.visibility === 'limited' ? page.public_url
  : page.active_url || (page.visibility === 'private' ? '/show/' + encodeURIComponent(sessionID) + '/'
    : page.share_id ? '/p/' + encodeURIComponent(page.share_id) + '/' : null);
return {allowed:true, url:href ? new URL(href, origin).href : '', visible:document.visibilityState === 'visible'};
"#;
