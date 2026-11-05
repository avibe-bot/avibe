# Motion Regression

Install the browser engines with `npx playwright install --with-deps chromium webkit`,
then run `npm run test:motion` from `ui/`.

This backend-free suite renders the shared surfaces that move
(docs/plans/web-ui-motion.md M6 and M9) on one fixture page: a `Dialog` (a
sheet on a phone), a `ResponsiveMenu`, a `Popover`, and toasts from the
production `ToastProvider`. It runs on a desktop and a phone in Chromium and on
a phone in WebKit.

It checks what each surface plays as it opens and closes, on each screen and
under reduced motion; that a popover grows from its trigger; that a phone sheet
springs back from a short pull, dismisses past 30% of its height or on a flick
(leaving from where the finger let go), and leaves scrolled content to scroll;
and that a toast rises, the stack lifts a beat ahead of it, a dismissed toast
leaves before it is gone, and a finger swipes one away. Dragging uses
Chromium's touch emulation, so those cases skip WebKit. Frame rate on an iPhone
is a device check.

All API requests are blocked. Failure traces are written under
`e2e/.artifacts/motion/`.
