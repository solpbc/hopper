# tmux + Claude Code: Programmatic State Discovery

Research notes on reading Claude Code state from the system without parsing terminal output.

## Terminal Title (Best Source)

Claude Code sets the terminal title via OSC escape sequences. tmux captures this in `#{pane_title}`.

```bash
tmux display-message -t :$WINDOW -p '#{pane_title}'
```

### Title Format

| Prefix | Meaning | Example |
|--------|---------|---------|
| `✳` (U+2733 sparkle) | Idle/waiting for input | `✳ Claude Code` |
| `◐`–`◓` (U+25D0–U+25D3 circles) | Active/processing | `◐ Fixing auth bug` |
| `⠐`/`⠂` (braille dots) | Active/processing | `⠐ Fixing auth bug` |

The circle and braille characters animate as spinners while Claude is working.

### Title Text Content

- **Default**: `Claude Code` (no summary generated yet)
- **Session summary**: Generated description like `Schema Simplification`
- **Active task**: Current work like `Background Research Command`

## tmux Commands Reference

### Session/Window Discovery

```bash
# List all windows with names and activity
tmux list-windows -F '#{window_index}: #{window_name} active=#{window_active}'

# List all panes across session with process info
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} | #{window_name} | pid=#{pane_pid} | #{pane_current_command} | #{pane_current_path}'

# Get pane title (Claude state)
tmux list-panes -a -F '#{window_index}: #{pane_title}'
```

### Useful Format Variables

| Variable | Description |
|----------|-------------|
| `#{pane_title}` | Terminal title set by application (Claude state) |
| `#{pane_pid}` | PID of shell in pane |
| `#{pane_current_command}` | Current foreground command |
| `#{pane_current_path}` | Current working directory |
| `#{window_name}` | Window name |
| `#{window_activity_string}` | Last activity timestamp |
| `#{history_size}` | Scrollback buffer line count |

### Capture Visible Content

```bash
# Capture last N lines from a window
tmux capture-pane -t :$WINDOW -p | tail -$N
```

## Claude Code File-Based State

### Sessions Index

Location: `~/.claude/projects/<project-path>/sessions-index.json`

```json
{
  "version": 1,
  "entries": [
    {
      "sessionId": "uuid",
      "fullPath": "/path/to/session.jsonl",
      "firstPrompt": "initial user message",
      "summary": "Generated session summary",
      "messageCount": 53,
      "created": "ISO timestamp",
      "modified": "ISO timestamp",
      "gitBranch": "main",
      "projectPath": "/path/to/project"
    }
  ]
}
```

Updated when sessions end, not real-time.

### Session Log

Location: `~/.claude/projects/<project-path>/<sessionId>.jsonl`

JSONL format, one record per line. Record types include:
- `file-history-snapshot`
- `user` / `assistant` messages
- `tool_use` / `tool_result`
- `progress` / `hook_progress`

Updated in real-time during session.

### Debug Log

Location: `~/.claude/debug/<sessionId>.txt`

Symlink: `~/.claude/debug/latest` points to current session's debug file.

```bash
# Get current session ID from debug symlink
basename $(readlink ~/.claude/debug/latest) .txt
```

### Project Structure

```
~/.claude/
├── projects/
│   └── <escaped-project-path>/
│       ├── sessions-index.json
│       ├── <sessionId>.jsonl
│       └── <sessionId>/
│           └── subagents/
├── debug/
│   ├── latest -> <sessionId>.txt
│   └── <sessionId>.txt
├── history.jsonl
├── settings.json
└── stats-cache.json
```

## Process Information

### Finding Claude Processes

```bash
# Process tree showing claude instances
ps --forest -eo pid,ppid,stat,etime,cmd | grep claude

# Check process name (always "claude", doesn't change with state)
cat /proc/$PID/comm
```

### Open Files

```bash
# See what files a claude process has open
lsof -p $PID | grep -E '\.json'
```

Note: Session `.jsonl` files are not kept open; only `history.jsonl`, `settings.json`, etc.

## What's NOT Available

- No environment variables exposing session ID or state
- Session ID not passed via command line arguments
- Process name (`/proc/pid/comm`) stays "claude" regardless of activity
- No dedicated status/pidfile
- No queryable IPC endpoint or socket

## Practical State Detection

```bash
#!/bin/bash
# Get Claude state for a tmux window

WINDOW=$1
TITLE=$(tmux display-message -t :$WINDOW -p '#{pane_title}')

# Parse state from icon
case "${TITLE:0:1}" in
  "✳") STATE="idle" ;;
  "⠐"|"⠂"|"⠈"|"⠁") STATE="active" ;;
  *) STATE="unknown" ;;
esac

# Extract description (after icon and space)
DESC="${TITLE:2}"

echo "Window $WINDOW: $STATE - $DESC"
```

## Example: List All Claude Windows

```bash
tmux list-panes -a -F '#{window_index}|#{pane_current_command}|#{pane_title}' | \
  grep '|claude|' | \
  while IFS='|' read win cmd title; do
    icon="${title:0:1}"
    desc="${title:2}"
    case "$icon" in
      "✳") state="idle" ;;
      *) state="active" ;;
    esac
    echo "Window $win: [$state] $desc"
  done
```
