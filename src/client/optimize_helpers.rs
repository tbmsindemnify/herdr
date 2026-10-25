//! Where the Optimize button's Python helpers live.
//!
//! The button, the usage meter, the native settings pane and the live review queue are Python
//! scripts shipped in this repository's `optimize/` directory. They are looked up, in order, in:
//! 1. `$HERDR_OPTIMIZE_HOME` when set;
//! 2. `~/.local/lib/herdr-personal` (the original installation layout);
//! 3. `$XDG_DATA_HOME/herdr/optimize`, defaulting to `~/.local/share/herdr/optimize` (what `install.sh` uses).

use std::path::PathBuf;

/// The directory holding the helper scripts, or `None` when there is no home directory at all.
pub fn helper_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("HERDR_OPTIMIZE_HOME").filter(|value| !value.is_empty()) {
        return Some(PathBuf::from(dir));
    }
    let home = PathBuf::from(std::env::var_os("HOME")?);
    let legacy = home.join(".local/lib/herdr-personal");
    if legacy.join("workspace_optimize.py").is_file() {
        return Some(legacy);
    }
    let data_home = std::env::var_os("XDG_DATA_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"));
    Some(data_home.join("herdr/optimize"))
}

/// Full path of one helper script, e.g. `helper("workspace_optimize.py")`.
pub fn helper(name: &str) -> Option<PathBuf> {
    helper_dir().map(|dir| dir.join(name))
}
