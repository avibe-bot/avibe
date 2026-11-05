# Apple sign-in button artwork

Downloaded from Apple's official button generator on 2026-11-05 for static design reference.
Source: https://developer.apple.com/documentation/signinwithapple/incorporating-sign-in-with-apple-into-other-platforms
Endpoint: https://appleid.cdn-apple.com/appleid/button
Parameters: `height=52&width=350&color={black|white}&border=false&type=sign-in&border_radius=12&scale=2&locale={en_US|zh_CN}`.

The PNGs preserve Apple's logo, localized label, alignment and neutral colors. The iOS implementation must use Apple's native Sign in with Apple control. These assets do not implement authentication. At narrower widths preserve the aspect ratio; native controls should instead be laid out at their intrinsic 52-point height.
