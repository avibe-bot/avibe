// What this scan can read as a CSS length.
//
// The comment this used to sit under names the shape of its own bug three times
// over: "each time the predicate checked something narrower than the error
// message claimed: first the colour, by listing two literal spellings out of the
// many CSS accepts; then the input, by reading one of the four channels a shadow
// value arrives through; then the geometry". The line directly beneath it then
// listed six units out of the thirty-odd CSS accepts, and the fourth instance
// cost what the first three did: `box-shadow: 1cm 1cm 4px red` -- a directional
// shadow, no glow anywhere in it -- was reported as "the offsets are not plain
// lengths" and failed `validate:theme` for a pull request that was correct.
//
// `pt`, `vmin`, `lh`, `Q` and every viewport unit added since do the same. So
// the enumeration is gone rather than extended to thirty: a CSS length is a
// number followed by a unit, and stating the grammar covers the units nobody
// has thought of yet -- which is the only version of this that stops being
// wrong. Over-accepting is the safe direction here: an unrecognised length is
// reported as unreadable, and that is a false positive, while a value this
// admits and CSS does not still has to survive the zero and colour tests below.
//
// `i`, because CSS units are case-insensitive and one of them, `Q`, is normally
// written in the case this pattern would otherwise reject outright.
const LENGTH = /^[+-]?(\d+(\.\d+)?|\.\d+)(e[+-]?\d+)?[a-z]*$/i;

const isLength = (part) => LENGTH.test(part);

// Zero is a number, not a spelling. This used to be a regex matching the literal
// `0` with an optional unit, which is one of the ways to write zero out of
// several: `-0px`, `+0px`, `0.0`, `00px` and `0vw` are all the same offset, and
// each of them was read as a DIRECTIONAL offset -- so `shadow-[-0px_0_93px_red]`
// took the exemption that exists for light thrown to one side and drew a
// centred glow with it. The exemption is the dangerous side of that question,
// which is what makes the narrow spelling expensive rather than untidy. Every
// caller has already established through isLength that the part is a length, so
// the numeric component is whatever precedes the unit and the question can be
// asked as arithmetic, where all the spellings converge by construction.
const isZeroLength = (part) => Number.parseFloat(part) === 0;

export { isLength, isZeroLength };
