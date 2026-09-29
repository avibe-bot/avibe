//! Where a restored window may appear.
//!
//! A restored window stays exactly where the user left it — across two
//! displays, partly past an edge, anywhere — as long as the user can still grab
//! its title bar and drag it. Only a window that can no longer be grabbed, for
//! example because the display it was on is gone, is moved.

/// A rectangle in the physical pixels the window reports its position and size
/// in: the window's top-left corner and size, or a display's work area.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WindowFrame {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

/// The grab handle: the part of the title bar that drags the window on every
/// frame the shell draws, in logical px from the window's top-left corner.
///
/// The macOS sidebar reserves at least 248 × 28 pt of draggable clearance
/// (`DesktopDragRegion` in the Workbench). Other header areas can contain
/// controls, so recovery conservatively keeps this guaranteed area reachable.
/// It starts past the traffic lights, which end
/// about 60 pt from the left edge, and past the Windows window icon. The
/// standard macOS title bar and the Windows caption drag across all of it.
const HANDLE_LEFT: f64 = 72.0;
const HANDLE_RIGHT: f64 = 248.0;
const HANDLE_HEIGHT: f64 = 28.0;

/// The smallest square of the handle that still counts as grabbable, in logical
/// px: WCAG 2.2's minimum pointer target (success criterion 2.5.8, 24 × 24 CSS
/// px). A window showing less of its handle is a sliver the user can barely
/// catch, so it is pulled in.
const GRAB_SIZE: f64 = 24.0;

/// Restores `frame` onto the displays whose work areas are `work_areas`.
///
/// The frame and the work areas share one coordinate space, in which a logical
/// px is `scale_factor` physical px: the window's own scale factor, because the
/// window draws its title bar and enforces its minimum size at that scale, not
/// at the scale of each display it overlaps.
///
/// A frame whose grab handle shows a grabbable square anywhere on the union of
/// the work areas keeps its position, so a frame spanning two displays
/// round-trips unchanged. Any other frame moves into the work area it overlaps
/// most, or the nearest one when it overlaps none, just far enough to show its
/// whole handle and, when it fits, its whole width. Either way its size grows
/// to at least `minimum`, in logical px.
pub fn clamp_window_frame(
    frame: WindowFrame,
    scale_factor: f64,
    work_areas: &[WindowFrame],
    minimum: (f64, f64),
) -> WindowFrame {
    let frame = WindowFrame {
        width: frame.width.max(to_physical(minimum.0, scale_factor)),
        height: frame.height.max(to_physical(minimum.1, scale_factor)),
        ..frame
    };
    let work_areas: Vec<WindowFrame> = work_areas
        .iter()
        .copied()
        .filter(|area| area.width > 0 && area.height > 0)
        .collect();
    if is_grabbable(frame, scale_factor, &work_areas) {
        return frame;
    }
    work_areas
        .iter()
        .map(|area| {
            let overlap = Rect::of(frame).intersection(Rect::of(*area)).map_or(0.0, Rect::area);
            let pulled = pull_into(frame, scale_factor, *area);
            let dx = i128::from(pulled.x) - i128::from(frame.x);
            let dy = i128::from(pulled.y) - i128::from(frame.y);
            (overlap, dx * dx + dy * dy, pulled)
        })
        .max_by(|a, b| a.0.total_cmp(&b.0).then(b.1.cmp(&a.1)))
        .map_or(frame, |(_, _, pulled)| pulled)
}

fn to_physical(logical: f64, scale_factor: f64) -> u32 {
    (logical * scale_factor).ceil() as u32
}

/// Moves `frame` as little as possible for `area` to show its whole handle
/// and, when it fits, its whole width.
fn pull_into(frame: WindowFrame, scale_factor: f64, area: WindowFrame) -> WindowFrame {
    let clamp_position = |position: i32, start: i32, extent: u32, visible: u32| {
        let start = i64::from(start);
        let end = start + i64::from(extent.saturating_sub(visible));
        i64::from(position)
            .clamp(start, end)
            .clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32
    };
    let handle_width = to_physical(HANDLE_RIGHT, scale_factor);
    let handle_height = to_physical(HANDLE_HEIGHT, scale_factor);
    WindowFrame {
        x: clamp_position(frame.x, area.x, area.width, frame.width.max(handle_width)),
        y: clamp_position(frame.y, area.y, area.height, handle_height),
        ..frame
    }
}

/// Whether a grab-sized square of the frame's handle lies on the union of the
/// work areas, possibly across the seam between two of them.
fn is_grabbable(frame: WindowFrame, scale_factor: f64, work_areas: &[WindowFrame]) -> bool {
    let (x, y) = (f64::from(frame.x), f64::from(frame.y));
    let handle = Rect {
        left: x + HANDLE_LEFT * scale_factor,
        top: y,
        right: x + HANDLE_RIGHT * scale_factor,
        bottom: y + HANDLE_HEIGHT * scale_factor,
    };
    let grab = GRAB_SIZE * scale_factor;
    let shown: Vec<Rect> = work_areas
        .iter()
        .filter_map(|area| Rect::of(*area).intersection(handle))
        .collect();
    // A square lying on the shown pieces can slide left, then up, and stay on
    // them until its left edge meets the left edge of a piece and its top edge
    // the top edge of a piece. Those corners are the only places to look.
    shown.iter().any(|left| {
        shown.iter().any(|top| {
            let square = Rect {
                left: left.left,
                top: top.top,
                right: left.left + grab,
                bottom: top.top + grab,
            };
            is_covered(square, &shown)
        })
    })
}

/// Whether `pieces` together cover `target`: each cell of the grid their edges
/// cut `target` into lies inside one piece.
fn is_covered(target: Rect, pieces: &[Rect]) -> bool {
    let cuts = |start: f64, end: f64, edges: fn(&Rect) -> [f64; 2]| {
        let mut cuts: Vec<f64> = pieces
            .iter()
            .flat_map(edges)
            .filter(|&edge| start < edge && edge < end)
            .chain([start, end])
            .collect();
        cuts.sort_by(f64::total_cmp);
        cuts.dedup();
        cuts
    };
    let columns = cuts(target.left, target.right, |piece| [piece.left, piece.right]);
    let rows = cuts(target.top, target.bottom, |piece| [piece.top, piece.bottom]);
    columns.windows(2).all(|column| {
        rows.windows(2).all(|row| {
            let cell = Rect {
                left: column[0],
                top: row[0],
                right: column[1],
                bottom: row[1],
            };
            pieces.iter().any(|piece| piece.contains(cell))
        })
    })
}

/// A half-open rectangle `[left, right) × [top, bottom)` in physical px, whose
/// edges may fall between pixels at fractional scale factors.
#[derive(Clone, Copy, Debug)]
struct Rect {
    left: f64,
    top: f64,
    right: f64,
    bottom: f64,
}

impl Rect {
    fn of(frame: WindowFrame) -> Self {
        let (left, top) = (f64::from(frame.x), f64::from(frame.y));
        Self {
            left,
            top,
            right: left + f64::from(frame.width),
            bottom: top + f64::from(frame.height),
        }
    }

    fn intersection(self, other: Self) -> Option<Self> {
        let overlap = Self {
            left: self.left.max(other.left),
            top: self.top.max(other.top),
            right: self.right.min(other.right),
            bottom: self.bottom.min(other.bottom),
        };
        (overlap.left < overlap.right && overlap.top < overlap.bottom).then_some(overlap)
    }

    fn contains(self, other: Self) -> bool {
        self.left <= other.left && other.right <= self.right && self.top <= other.top && other.bottom <= self.bottom
    }

    fn area(self) -> f64 {
        (self.right - self.left) * (self.bottom - self.top)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMUM: (f64, f64) = (880.0, 600.0);

    fn rect(x: i32, y: i32, width: u32, height: u32) -> WindowFrame {
        WindowFrame { x, y, width, height }
    }

    /// Reference for "grabbable", independent of the geometry above: paints the
    /// handle pixel by pixel and looks for a grab-sized square of on-screen
    /// pixels. Exact for the scale factors the tests use, which put every
    /// handle edge on a pixel boundary.
    fn grabbable_by_raster(frame: WindowFrame, scale_factor: f64, work_areas: &[WindowFrame]) -> bool {
        let px = |logical: f64| (logical * scale_factor) as i64;
        let (left, top) = (i64::from(frame.x) + px(HANDLE_LEFT), i64::from(frame.y));
        let (width, height) = (px(HANDLE_RIGHT - HANDLE_LEFT) as usize, px(HANDLE_HEIGHT));
        let grab = px(GRAB_SIZE) as usize;
        // side[column] is the side of the largest on-screen square whose
        // bottom-right pixel is that column of the current row.
        let mut side = vec![0_usize; width + 1];
        let mut on_screen = vec![false; width];
        for row in top..top + height {
            on_screen.fill(false);
            for area in work_areas {
                let (area_left, area_top) = (i64::from(area.x), i64::from(area.y));
                if !(area_top..area_top + i64::from(area.height)).contains(&row) {
                    continue;
                }
                let start = (area_left - left).clamp(0, width as i64) as usize;
                let end = (area_left + i64::from(area.width) - left).clamp(0, width as i64) as usize;
                on_screen[start..end].fill(true);
            }
            if !on_screen.contains(&true) {
                side.fill(0);
                continue;
            }
            let mut diagonal = 0;
            for column in 1..=width {
                let above = side[column];
                side[column] = if on_screen[column - 1] {
                    1 + above.min(side[column - 1]).min(diagonal)
                } else {
                    0
                };
                diagonal = above;
                if side[column] >= grab {
                    return true;
                }
            }
        }
        false
    }

    /// Display layouts: side by side, with gaps, of different heights, stacked,
    /// with the primary display anywhere in the arrangement.
    fn layouts() -> Vec<Vec<WindowFrame>> {
        let mut layouts = vec![
            vec![rect(0, 0, 1920, 1080)],
            vec![rect(0, 0, 1920, 1080), rect(1920, 0, 1920, 1080)],
            vec![rect(0, 25, 1512, 957), rect(1512, 0, 2560, 1415)],
            vec![
                rect(-2560, -360, 2560, 1440),
                rect(0, 0, 1920, 1080),
                rect(1920, 200, 1080, 1920),
            ],
            vec![rect(0, 0, 1920, 1040), rect(0, 1080, 1920, 1040)],
            vec![rect(0, 0, 2560, 1440), rect(320, 1440, 1920, 1080)],
            vec![rect(0, 0, 1920, 1080), rect(2000, 100, 1280, 1024)],
            vec![rect(0, 0, 3840, 2160), rect(3840, 1080, 1920, 1080)],
        ];
        for seed in 1..10_i32 {
            let first = rect(
                -seed * 37,
                seed * 13,
                (480 + seed * 170) as u32,
                (320 + seed * 110) as u32,
            );
            let second = rect(
                first.x + first.width as i32 + seed % 3 * 40,
                first.y - 150 + seed * 30,
                (900 + seed * 90) as u32,
                (500 + seed * 70) as u32,
            );
            layouts.push(vec![first, second]);
        }
        layouts
    }

    /// Positions where the handle shows one px less than, exactly, or one px
    /// more than a grab-sized square past each work area edge, where it
    /// straddles each leading edge half and half, the centre of each work area,
    /// and positions far off screen: each along one axis, and every other one
    /// of them combined on both.
    fn probe_positions(work_areas: &[WindowFrame], scale_factor: f64) -> Vec<(i32, i32)> {
        let px = |logical: f64| (logical * scale_factor) as i32;
        let mut xs = vec![i32::MIN, -10_000, 10_000, i32::MAX];
        let mut ys = xs.clone();
        for area in work_areas {
            let (right, bottom) = (area.x + area.width as i32, area.y + area.height as i32);
            for delta in -1..=1 {
                xs.push(right - px(HANDLE_LEFT + GRAB_SIZE) + delta);
                xs.push(area.x - px(HANDLE_RIGHT - GRAB_SIZE) + delta);
                ys.push(area.y - px(HANDLE_HEIGHT - GRAB_SIZE) + delta);
                ys.push(bottom - px(GRAB_SIZE) + delta);
            }
            xs.push(area.x - px((HANDLE_LEFT + HANDLE_RIGHT) / 2.0));
            ys.push(area.y - px(HANDLE_HEIGHT / 2.0));
            xs.push(area.x + area.width as i32 / 2);
            ys.push(area.y + area.height as i32 / 2);
        }
        let home = work_areas[0];
        let (x_centre, y_centre) = (home.x + home.width as i32 / 2, home.y + home.height as i32 / 2);
        let mut positions: Vec<(i32, i32)> = xs.iter().map(|&x| (x, y_centre)).collect();
        positions.extend(ys.iter().map(|&y| (x_centre, y)));
        for &x in xs.iter().step_by(2) {
            positions.extend(ys.iter().step_by(2).map(|&y| (x, y)));
        }
        positions
    }

    #[test]
    fn every_restored_frame_is_grabbable_at_least_minimum_and_stable() {
        let mut cases = 0;
        for work_areas in layouts() {
            for scale_factor in [1.0, 1.25, 1.5, 2.0] {
                let minimum = (
                    (MINIMUM.0 * scale_factor).ceil() as u32,
                    (MINIMUM.1 * scale_factor).ceil() as u32,
                );
                let handle = (
                    (HANDLE_RIGHT * scale_factor) as i64,
                    (HANDLE_HEIGHT * scale_factor) as i64,
                );
                for (index, (x, y)) in probe_positions(&work_areas, scale_factor).into_iter().enumerate() {
                    let index = index as u32;
                    let saved = rect(x, y, 300 + index * 97 % 1900, 200 + index * 61 % 1200);
                    let restored = clamp_window_frame(saved, scale_factor, &work_areas, MINIMUM);
                    let case = format!("{saved:?} at {scale_factor}x on {work_areas:?} -> {restored:?}");
                    assert!(grabbable_by_raster(restored, scale_factor, &work_areas), "{case}");
                    if grabbable_by_raster(saved, scale_factor, &work_areas) {
                        assert_eq!((restored.x, restored.y), (saved.x, saved.y), "{case}");
                    } else {
                        let (x, y) = (i64::from(restored.x), i64::from(restored.y));
                        assert!(
                            work_areas.iter().any(|area| {
                                let (left, top) = (i64::from(area.x), i64::from(area.y));
                                left <= x
                                    && x + handle.0 <= left + i64::from(area.width)
                                    && top <= y
                                    && y + handle.1 <= top + i64::from(area.height)
                            }),
                            "a pulled-in frame shows its whole title bar strip on one display: {case}"
                        );
                    }
                    assert_eq!(
                        (restored.width, restored.height),
                        (saved.width.max(minimum.0), saved.height.max(minimum.1)),
                        "{case}"
                    );
                    assert_eq!(
                        clamp_window_frame(restored, scale_factor, &work_areas, MINIMUM),
                        restored,
                        "{case}"
                    );
                    cases += 1;
                }
            }
        }
        assert!(cases > 5_000, "{cases}");
    }

    #[test]
    fn a_frame_spanning_two_displays_round_trips_unchanged() {
        let work_areas = [rect(0, 0, 1920, 1080), rect(1920, 0, 1920, 1080)];
        let frame = rect(1600, 200, 1000, 700);
        let restored = clamp_window_frame(frame, 1.0, &work_areas, MINIMUM);
        assert_eq!(restored, frame);
        assert_eq!(clamp_window_frame(restored, 1.0, &work_areas, MINIMUM), restored);
    }

    #[test]
    fn an_accessible_frame_is_unchanged_across_restore_and_show() {
        let work_areas = [rect(-2000, 40, 2000, 1400), rect(0, 40, 2000, 1400)];
        let frame = rect(-1800, 200, 1000, 700);
        assert_eq!(clamp_window_frame(frame, 1.0, &work_areas, MINIMUM), frame);
        assert_eq!(
            clamp_window_frame(frame, 1.0, &work_areas[1..], MINIMUM),
            rect(0, 200, 1000, 700)
        );
    }

    #[test]
    fn a_frame_left_on_a_removed_display_moves_onto_the_nearest_live_one() {
        let live = [rect(0, 0, 1920, 1080), rect(0, 1080, 1920, 1080)];
        // It was on a display to the right of the upper one.
        let restored = clamp_window_frame(rect(2400, 300, 1000, 700), 1.0, &live, MINIMUM);
        assert_eq!(restored, rect(920, 300, 1000, 700));
        // It was on a display below both.
        let restored = clamp_window_frame(rect(100, 5000, 1000, 700), 1.0, &live, MINIMUM);
        assert_eq!(restored, rect(100, 2160 - 28, 1000, 700));
    }

    #[test]
    fn a_handle_showing_only_a_sliver_is_pulled_in() {
        let work_areas = [rect(0, 0, 1920, 1080)];
        // Past the right edge, the handle (72 to 248 px from the left) shows 23
        // columns, then 24.
        let sliver = rect(1920 - 72 - 23, 300, 1000, 700);
        assert_eq!(
            clamp_window_frame(sliver, 1.0, &work_areas, MINIMUM),
            rect(920, 300, 1000, 700)
        );
        let grabbable = rect(1920 - 72 - 24, 300, 1000, 700);
        assert_eq!(clamp_window_frame(grabbable, 1.0, &work_areas, MINIMUM), grabbable);
        // Past the left edge, it shows 23 columns, then 24.
        let sliver = rect(23 - 248, 300, 1000, 700);
        assert_eq!(clamp_window_frame(sliver, 1.0, &work_areas, MINIMUM).x, 0);
        let grabbable = rect(24 - 248, 300, 1000, 700);
        assert_eq!(clamp_window_frame(grabbable, 1.0, &work_areas, MINIMUM), grabbable);
        // Above the top edge, the 28 px tall handle shows 23 rows, then 24.
        let sliver = rect(300, -5, 1000, 700);
        assert_eq!(clamp_window_frame(sliver, 1.0, &work_areas, MINIMUM).y, 0);
        let grabbable = rect(300, -4, 1000, 700);
        assert_eq!(clamp_window_frame(grabbable, 1.0, &work_areas, MINIMUM), grabbable);
        // Past the bottom edge, it shows 23 rows, then 24.
        let sliver = rect(300, 1080 - 23, 1000, 700);
        assert_eq!(clamp_window_frame(sliver, 1.0, &work_areas, MINIMUM).y, 1080 - 28);
        let grabbable = rect(300, 1080 - 24, 1000, 700);
        assert_eq!(clamp_window_frame(grabbable, 1.0, &work_areas, MINIMUM), grabbable);
    }

    #[test]
    fn the_handle_and_minimum_scale_with_the_window() {
        // A 1x display beside a 2x one, in the one physical desktop Windows
        // reports; the window's own scale decides every size.
        let work_areas = [rect(0, 0, 1920, 1080), rect(1920, 0, 3840, 2160)];
        // At 2x the handle starts 144 px in and the grab square is 48 px, so 40
        // columns of handle on the right edge are a sliver; at 1x the same
        // frame shows 112 columns.
        let frame = rect(1920 + 3840 - 144 - 40, 300, 2000, 1400);
        assert_eq!(clamp_window_frame(frame, 1.0, &work_areas, MINIMUM), frame);
        assert_eq!(
            clamp_window_frame(frame, 2.0, &work_areas, MINIMUM),
            rect(1920 + 3840 - 2000, 300, 2000, 1400)
        );
        // A window straddling both displays keeps its place; only its size
        // grows to the minimum at its own scale.
        let spanning = rect(1700, 300, 1000, 700);
        assert_eq!(
            clamp_window_frame(spanning, 2.0, &work_areas, MINIMUM),
            rect(1700, 300, 1760, 1200)
        );
    }

    #[test]
    fn a_handle_partly_in_the_gap_between_displays_of_different_heights() {
        // A tall display on the left, a short one on the right: below the short
        // display's bottom (1080) the right part of the handle is in the gap.
        let work_areas = [rect(0, 0, 2560, 1440), rect(2560, 0, 1920, 1080)];
        // 20 columns of handle on the tall display and 10 rows on the short one
        // do not add up to a grab square.
        let sliver = rect(2560 - 72 - 20, 1070, 1000, 700);
        assert_eq!(
            clamp_window_frame(sliver, 1.0, &work_areas, MINIMUM),
            rect(2560 - 1000, 1070, 1000, 700)
        );
        // 60 columns on the tall display do.
        let grabbable = rect(2560 - 72 - 60, 1070, 1000, 700);
        assert_eq!(clamp_window_frame(grabbable, 1.0, &work_areas, MINIMUM), grabbable);
    }

    #[test]
    fn a_handle_across_the_seam_of_stacked_displays_is_grabbable() {
        let work_areas = [rect(0, 0, 1920, 1080), rect(0, 1080, 1920, 1080)];
        // 14 rows of handle above the seam and 14 below: neither display alone
        // shows a grab square, together they show the whole handle.
        let straddling = rect(400, 1080 - 14, 1000, 700);
        assert_eq!(clamp_window_frame(straddling, 1.0, &work_areas, MINIMUM), straddling);
        // With a gap between the displays, the frame is pulled onto the lower
        // one, which holds most of it.
        let gapped = [rect(0, 0, 1920, 1080), rect(0, 1120, 1920, 1080)];
        assert_eq!(
            clamp_window_frame(straddling, 1.0, &gapped, MINIMUM),
            rect(400, 1120, 1000, 700)
        );
    }
}
