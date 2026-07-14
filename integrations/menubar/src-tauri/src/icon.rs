//! Runtime-drawn tray icon: a little head that is also a battery.
//!
//! The head silhouette (a minifig-style head with a stud on top) doubles as
//! the battery body; the fill level rising inside it is the fleet's average
//! 5h battery — literally "how much headroom is left". Drawn as a
//! template image (black shapes + alpha; macOS recolors template icons for
//! menu-bar appearance, Windows gets white shapes so the taskbar stays
//! legible). `None` means "no current reading" and renders the empty head
//! with a dash where the level would be.

use tiny_skia::{FillRule, Mask, Paint, PathBuilder, Pixmap, Rect, Transform};

/// Canvas edge in pixels: 22pt at @2x, the standard macOS menu-bar raster.
pub const SIZE: u32 = 44;

/// One rounded rectangle as a path (tiny-skia has no rounded-rect helper).
fn rounded_rect(x: f32, y: f32, w: f32, h: f32, r: f32) -> tiny_skia::Path {
    let r = r.min(w / 2.0).min(h / 2.0);
    let mut pb = PathBuilder::new();
    pb.move_to(x + r, y);
    pb.line_to(x + w - r, y);
    pb.quad_to(x + w, y, x + w, y + r);
    pb.line_to(x + w, y + h - r);
    pb.quad_to(x + w, y + h, x + w - r, y + h);
    pb.line_to(x + r, y + h);
    pb.quad_to(x, y + h, x, y + h - r);
    pb.line_to(x, y + r);
    pb.quad_to(x, y, x + r, y);
    pb.close();
    pb.finish().expect("rounded rect is a valid path")
}

/// Render the icon as straight (non-premultiplied) RGBA bytes.
///
/// `level` is the fill fraction in `0.0..=1.0`; `None` draws the
/// no-current-reading dash.
pub fn tray_icon_rgba(level: Option<f32>) -> (Vec<u8>, u32, u32) {
    let mut pixmap = Pixmap::new(SIZE, SIZE).expect("tray icon pixmap");
    let mut paint = Paint::default();
    // Template ink: black on macOS/Linux (the OS recolors it), white on
    // Windows so the icon reads on the dark taskbar.
    let ink = if cfg!(windows) { 255 } else { 0 };
    paint.set_color_rgba8(ink, ink, ink, 255);
    paint.anti_alias = true;

    // Stud on top: the piece that makes it a minifig head.
    let stud = rounded_rect(16.0, 3.0, 12.0, 8.0, 2.5);
    pixmap.fill_path(&stud, &paint, FillRule::Winding, Transform::identity(), None);

    // Head outline: outer silhouette minus an inner hole (even-odd), so the
    // head is a 3px-walled tank.
    let outer = rounded_rect(7.0, 9.0, 30.0, 32.0, 9.0);
    let inner = rounded_rect(10.0, 12.0, 24.0, 26.0, 6.5);
    let mut ring = PathBuilder::new();
    ring.push_path(&outer);
    ring.push_path(&inner);
    let ring = ring.finish().expect("head ring path");
    pixmap.fill_path(&ring, &paint, FillRule::EvenOdd, Transform::identity(), None);

    // The tank interior the level fills (inset from the wall for a gap).
    let tank = rounded_rect(12.0, 14.0, 20.0, 22.0, 5.0);
    match level {
        Some(level) => {
            let level = level.clamp(0.0, 1.0);
            if level > 0.0 {
                // Fill from the bottom, clipped to the tank's rounded shape.
                let mut mask = Mask::new(SIZE, SIZE).expect("tank mask");
                mask.fill_path(&tank, FillRule::Winding, true, Transform::identity());
                let height = 22.0 * level;
                if let Some(fill) = Rect::from_xywh(12.0, 14.0 + (22.0 - height), 20.0, height)
                {
                    pixmap.fill_rect(fill, &paint, Transform::identity(), Some(&mask));
                }
            }
        }
        None => {
            // No current reading: a dash across the middle of the tank.
            let dash = rounded_rect(15.0, 23.5, 14.0, 3.0, 1.5);
            pixmap.fill_path(&dash, &paint, FillRule::Winding, Transform::identity(), None);
        }
    }

    // tiny-skia stores premultiplied RGBA; hand tauri straight RGBA.
    let data = pixmap
        .pixels()
        .iter()
        .flat_map(|px| {
            let px = px.demultiply();
            [px.red(), px.green(), px.blue(), px.alpha()]
        })
        .collect();
    (data, SIZE, SIZE)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn alpha_sum(rgba: &[u8]) -> u64 {
        rgba.chunks_exact(4).map(|px| px[3] as u64).sum()
    }

    #[test]
    fn levels_render_and_more_level_means_more_ink() {
        let (empty, w, h) = tray_icon_rgba(Some(0.0));
        assert_eq!((w, h), (SIZE, SIZE));
        assert_eq!(empty.len(), (SIZE * SIZE * 4) as usize);
        let (half, ..) = tray_icon_rgba(Some(0.5));
        let (full, ..) = tray_icon_rgba(Some(1.0));
        assert!(alpha_sum(&empty) < alpha_sum(&half));
        assert!(alpha_sum(&half) < alpha_sum(&full));
    }

    #[test]
    fn unknown_reading_draws_the_dash_not_a_fill() {
        let (unknown, ..) = tray_icon_rgba(None);
        let (empty, ..) = tray_icon_rgba(Some(0.0));
        let (full, ..) = tray_icon_rgba(Some(1.0));
        assert!(alpha_sum(&unknown) > alpha_sum(&empty));
        assert!(alpha_sum(&unknown) < alpha_sum(&full));
    }

    #[test]
    fn out_of_range_levels_clamp_instead_of_panicking() {
        let (over, ..) = tray_icon_rgba(Some(7.0));
        let (full, ..) = tray_icon_rgba(Some(1.0));
        assert_eq!(alpha_sum(&over), alpha_sum(&full));
        let (under, ..) = tray_icon_rgba(Some(-3.0));
        let (empty, ..) = tray_icon_rgba(Some(0.0));
        assert_eq!(alpha_sum(&under), alpha_sum(&empty));
    }
}
