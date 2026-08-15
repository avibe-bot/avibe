// How long a test that walks the whole UI tree may run before it is treated as
// hung.
//
// vitest's 5s default is a UNIT test's clock, and these are not unit tests. They
// read and parse every source file in the repository on purpose: the properties
// they assert -- that blanking non-rendering text preserves every file's length,
// that the glow scale can spell every blur design.pen annotates, that the lint
// domain resolves one policy for every file in it -- are about the real tree and
// cannot be stated over fixtures. Three of the files they cover contain astral
// characters, which is exactly the case a hand-written fixture does not have.
//
// So the number is a hang detector, not a performance budget. The work is under
// two seconds on a developer machine; a CI runner executing 213 test files over
// a shared pair of cores multiplies that enough to cross five, which is how
// these tests spent six pushes failing intermittently -- red CI naming no defect
// at all, on the one gate whose whole purpose is to tell a defect from a
// correct file. Under a minute is contention. Over it is a loop that will not
// end on its own.
export const WHOLE_TREE_SCAN = 60_000;
