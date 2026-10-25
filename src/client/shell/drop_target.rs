//! Route a file dropped onto the host terminal to the pane under the pointer.
//!
//! foot turns a drag-and-drop into a bracketed paste of the file paths, and
//! Wayland sends no pointer motion to the terminal while a drag is in
//! progress, so Herdr's own hover state is stale at drop time. On a paste
//! that is nothing but existing absolute paths, ask the compositor where the
//! pointer is and map it through the terminal window to a cell.

use std::path::Path;
use std::process::Command;

use super::ClientShellState;

/// True when `text` is one line made only of existing absolute file paths,
/// as foot pastes them for a drop (quoted or backslash-escaped).
pub(super) fn looks_like_file_drop(text: &str) -> bool {
    let line = text.trim_end_matches(['\r', '\n']);
    if line.is_empty() || line.contains(['\r', '\n']) {
        return false;
    }
    !drop_paths(line).is_empty()
}

/// The dropped paths, or empty when the line is not purely existing absolute
/// paths. foot may paste a path raw with unescaped spaces, so pieces split on
/// whitespace are rejoined until they name a file that exists.
fn drop_paths(line: &str) -> Vec<String> {
    let mut paths = Vec::new();
    let mut pending: Option<String> = None;
    for piece in split_drop_paths(line) {
        let candidate = match pending.take() {
            Some(prefix) => format!("{prefix} {piece}"),
            None => piece,
        };
        if !candidate.starts_with('/') {
            return Vec::new();
        }
        if Path::new(&candidate).exists() {
            paths.push(candidate);
        } else {
            pending = Some(candidate);
        }
    }
    if pending.is_some() {
        return Vec::new();
    }
    paths
}

/// Split a drop line into paths, honoring '…', "…" and backslash escapes, and
/// `file://` URIs.
fn split_drop_paths(line: &str) -> Vec<String> {
    let mut paths = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    let mut chars = line.chars();
    while let Some(ch) = chars.next() {
        match (quote, ch) {
            (Some(q), c) if c == q => quote = None,
            (None, '\'' | '"') => quote = Some(ch),
            (q, '\\') if q != Some('\'') => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (None, c) if c.is_whitespace() => {
                if !current.is_empty() {
                    paths.push(std::mem::take(&mut current));
                }
            }
            (_, c) => current.push(c),
        }
    }
    if !current.is_empty() {
        paths.push(current);
    }
    paths
        .into_iter()
        .map(|path| match path.strip_prefix("file://") {
            Some(rest) => percent_decode(rest),
            None => path,
        })
        .collect()
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(byte) = u8::from_str_radix(&text[i + 1..i + 3], 16) {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The pointer position in terminal cells, if the pointer is over the
/// terminal window that hosts this client.
fn pointer_cell(cell_width_px: u32, cell_height_px: u32) -> Option<(u16, u16)> {
    let cursor = hyprctl_json(&["cursorpos"])?;
    let (cx, cy) = (cursor.get("x")?.as_f64()?, cursor.get("y")?.as_f64()?);
    let host_pid = parent_pid()?;
    let clients = hyprctl_json(&["clients"])?;
    let window = clients
        .as_array()?
        .iter()
        .find(|client| client.get("pid").and_then(|p| p.as_i64()) == Some(host_pid))?;
    let at = window.get("at")?.as_array()?;
    let size = window.get("size")?.as_array()?;
    let (wx, wy) = (at.first()?.as_f64()?, at.get(1)?.as_f64()?);
    let (ww, wh) = (size.first()?.as_f64()?, size.get(1)?.as_f64()?);
    if cx < wx || cy < wy || cx >= wx + ww || cy >= wy + wh {
        return None;
    }
    let (pad_x, pad_y) = foot_padding();
    let col = (cx - wx - pad_x) / f64::from(cell_width_px.max(1));
    let row = (cy - wy - pad_y) / f64::from(cell_height_px.max(1));
    if col < 0.0 || row < 0.0 {
        return None;
    }
    Some((col as u16, row as u16))
}

fn hyprctl_json(args: &[&str]) -> Option<serde_json::Value> {
    let output = Command::new("hyprctl").arg("-j").args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    serde_json::from_slice(&output.stdout).ok()
}

#[cfg(unix)]
fn parent_pid() -> Option<i64> {
    let stat = std::fs::read_to_string("/proc/self/stat").ok()?;
    // Field 4 follows the parenthesized command name, which may contain spaces.
    let after_name = &stat[stat.rfind(')')? + 2..];
    after_name.split_whitespace().nth(1)?.parse().ok()
}

#[cfg(not(unix))]
fn parent_pid() -> Option<i64> {
    None
}

/// foot's `pad=WxH`; the grid starts this far from the window's top-left.
fn foot_padding() -> (f64, f64) {
    let Some(home) = std::env::var_os("HOME") else {
        return (0.0, 0.0);
    };
    let config = Path::new(&home).join(".config/foot/foot.ini");
    let Ok(text) = std::fs::read_to_string(config) else {
        return (0.0, 0.0);
    };
    text.lines()
        .map(str::trim)
        .find_map(|line| line.strip_prefix("pad=").or_else(|| line.strip_prefix("pad =")))
        .and_then(|value| {
            let value = value.trim().split_whitespace().next()?;
            let (x, y) = value.split_once('x')?;
            Some((x.trim().parse().ok()?, y.trim().parse().ok()?))
        })
        .unwrap_or((0.0, 0.0))
}

impl ClientShellState {
    /// Where a dropped image file goes: the pane under the pointer when it is
    /// a plain pane drop, otherwise the usual clipboard-image target.
    pub(crate) fn drop_image_target(&self) -> Option<crate::protocol::ClientClipboardImageTarget> {
        let fallback = self.clipboard_image_target()?;
        if !matches!(fallback, crate::protocol::ClientClipboardImageTarget::Pane(_)) {
            return Some(fallback);
        }
        Some(
            self.drop_target_pane()
                .map(crate::protocol::ClientClipboardImageTarget::Pane)
                .unwrap_or(fallback),
        )
    }

    /// The pane a dropped file should go to: the one under the pointer.
    pub(super) fn drop_target_pane(&self) -> Option<String> {
        let size = self.graphics_cell_size;
        let point = pointer_cell(size.width_px, size.height_px)?;
        self.hits
            .panes
            .iter()
            .find(|hit| super::contains(hit.inner_rect, point))
            .map(|hit| hit.pane_id.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_escaped_quoted_and_uri_paths() {
        assert_eq!(
            split_drop_paths(r"/tmp/a\ b.pdf '/tmp/c d.png' file:///tmp/e%20f.txt"),
            vec!["/tmp/a b.pdf", "/tmp/c d.png", "/tmp/e f.txt"]
        );
    }

    #[test]
    fn only_existing_absolute_paths_count_as_a_drop() {
        let dir = std::env::temp_dir();
        let file = dir.join("herdr drop test.txt");
        std::fs::write(&file, b"x").unwrap();
        let escaped = file.display().to_string().replace(' ', "\\ ");
        assert!(looks_like_file_drop(&escaped));
        assert!(looks_like_file_drop(&format!("'{}'\n", file.display())));
        assert!(!looks_like_file_drop("hello world"));
        assert!(!looks_like_file_drop("relative/path.txt"));
        assert!(!looks_like_file_drop("/definitely/not/here.pdf"));
        assert!(!looks_like_file_drop(&format!("{escaped}\nsecond line")));
        // foot can paste a dropped path raw, spaces unescaped, alone or beside others.
        assert!(looks_like_file_drop(&file.display().to_string()));
        assert_eq!(drop_paths(&format!("{} {}", file.display(), file.display())).len(), 2);
        std::fs::remove_file(file).unwrap();
    }
}
