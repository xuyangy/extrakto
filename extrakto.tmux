#!/usr/local/bin/bash

script_dir=$(
	builtin cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
	pwd
)

extrakto_open="$script_dir/scripts/open.sh"
extrakto_key="space"

lowercase_key=${extrakto_key,,}

if [[ $lowercase_key != "none" ]]; then
	tmux bind-key "${extrakto_key}" run-shell "\"$extrakto_open\" \"#{pane_id}\""
fi
