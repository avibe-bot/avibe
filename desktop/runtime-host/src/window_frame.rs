#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WindowFrame {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct MonitorArea {
    pub frame: WindowFrame,
    pub scale_factor: f64,
}

pub fn clamp_window_frame(frame: WindowFrame, monitors: &[MonitorArea], minimum: (f64, f64)) -> WindowFrame {
    let Some(monitor) = monitors
        .iter()
        .filter(|monitor| monitor.frame.width > 0 && monitor.frame.height > 0)
        .max_by_key(|monitor| overlap_area(frame, monitor.frame))
    else {
        return frame;
    };
    let bounds = monitor.frame;
    let width = frame.width.max((minimum.0 * monitor.scale_factor).ceil() as u32);
    let height = frame.height.max((minimum.1 * monitor.scale_factor).ceil() as u32);
    let title_bar = ((32.0 * monitor.scale_factor).ceil() as u32).min(bounds.height);
    let clamp_position = |position: i32, start: i32, extent: u32, visible: u32| {
        let start = i64::from(start);
        let end = start + i64::from(extent.saturating_sub(visible));
        i64::from(position)
            .clamp(start, end)
            .clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32
    };
    WindowFrame {
        x: clamp_position(frame.x, bounds.x, bounds.width, width),
        y: clamp_position(frame.y, bounds.y, bounds.height, title_bar),
        width,
        height,
    }
}

fn overlap_area(frame: WindowFrame, bounds: WindowFrame) -> u64 {
    let overlap = |start: i32, extent: u32, other_start: i32, other_extent: u32| {
        let right = (i64::from(start) + i64::from(extent)).min(i64::from(other_start) + i64::from(other_extent));
        (right - i64::from(start.max(other_start))).max(0) as u64
    };
    overlap(frame.x, frame.width, bounds.x, bounds.width) * overlap(frame.y, frame.height, bounds.y, bounds.height)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamping_makes_the_title_bar_visible_and_preserves_the_scaled_minimum() {
        let minimum = (880.0, 600.0);
        for seed in 1..200_u32 {
            let monitor = MonitorArea {
                frame: WindowFrame {
                    x: -(seed as i32) * 37,
                    y: seed as i32 * 13,
                    width: 480 + seed * 17,
                    height: 320 + seed * 11,
                },
                scale_factor: 1.0 + f64::from(seed % 5) * 0.5,
            };
            for position in [i32::MIN, -10_000, 0, 10_000, i32::MAX] {
                let saved = WindowFrame {
                    x: position,
                    y: position,
                    width: seed * 21,
                    height: seed * 15,
                };
                let restored = clamp_window_frame(saved, &[monitor], minimum);
                assert!(restored.width >= (minimum.0 * monitor.scale_factor).ceil() as u32);
                assert!(restored.height >= (minimum.1 * monitor.scale_factor).ceil() as u32);
                assert!(restored.width >= saved.width && restored.height >= saved.height);
                assert!(restored.x >= monitor.frame.x);
                assert!(i64::from(restored.x) < i64::from(monitor.frame.x) + i64::from(monitor.frame.width));
                assert!(restored.y >= monitor.frame.y);
                let title_bar = ((32.0 * monitor.scale_factor).ceil() as u32).min(monitor.frame.height);
                assert!(
                    i64::from(restored.y) + i64::from(title_bar)
                        <= i64::from(monitor.frame.y) + i64::from(monitor.frame.height)
                );
                assert_eq!(clamp_window_frame(restored, &[monitor], minimum), restored);
            }
        }
    }

    #[test]
    fn an_accessible_frame_is_unchanged_across_restore_and_show() {
        let monitors = [
            MonitorArea {
                frame: WindowFrame {
                    x: -2000,
                    y: 40,
                    width: 2000,
                    height: 1400,
                },
                scale_factor: 1.0,
            },
            MonitorArea {
                frame: WindowFrame {
                    x: 0,
                    y: 40,
                    width: 2000,
                    height: 1400,
                },
                scale_factor: 1.0,
            },
        ];
        let frame = WindowFrame {
            x: -1800,
            y: 200,
            width: 1000,
            height: 700,
        };
        assert_eq!(clamp_window_frame(frame, &monitors, (880.0, 600.0)), frame);
        assert!(clamp_window_frame(frame, &monitors[1..], (880.0, 600.0)).x >= 0);
    }
}
