# Sign in with Apple mark

`logo-white.png` is the unmodified image returned by Apple's official logo-only
button generator on 2026-11-05:

`https://appleid.cdn-apple.com/appleid/button/logo?height=28&width=28&color=white&border=false&border_radius=0&scale=2`

The generator returns a 60 × 60 PNG (its minimum size). The proposal displays it
at 28 × 28 inside the shipped `Button` size `lg`: the complete button shares the
adjacent action's 48px height, 14px label and `rounded-lg` radius. The official
Apple mark and required white/black palette remain intact; text uses the same
product typography as the adjacent action. Implementation uses the native Apple
control with this matching geometry.
