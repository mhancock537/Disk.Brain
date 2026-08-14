#!/bin/zsh
# Install the watch and drain LaunchAgents.
#
# Do NOT run this while the 22:00 pipeline job is working: an 11-hour
# enrichment holds the manifest, and a second writer during it is asking for
# trouble. Install after the pipeline reports complete.
set -eu
REPO="${0:A:h:h}"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$REPO/data/logs"

# The plists ship with a __REPO__ placeholder because launchd does not expand
# environment variables in ProgramArguments or WorkingDirectory. Substitute the
# real checkout path on the way in.
for label in watch drain web; do
  plist="com.diskbrain.$label.plist"
  sed "s|__REPO__|$REPO|g" "$REPO/scripts/$plist" > "$AGENTS/$plist"
  plutil -lint "$AGENTS/$plist"
  launchctl bootout "gui/$(id -u)/com.diskbrain.$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$plist"
  print -r -- "installed com.diskbrain.$label"
done

print -r -- ""
print -r -- "watch: runs continuously, cheap work only, never touches the GPU"
print -r -- "drain: 02:00 daily, capped at [drain] max_documents in config.toml"
print -r -- "web:   always on at http://127.0.0.1:8765, restarts if it dies"
print -r -- ""
print -r -- "Remove with:  launchctl bootout gui/\$(id -u)/com.diskbrain.watch"
