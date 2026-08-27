#!/usr/local/bin/bash

script_dir=$(
	builtin cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
	pwd
)

python_bin="$HOME/.pyenv/versions/3.13.12/bin/python3"
extrakto_key="space"

lowercase_key=${extrakto_key,,}

if [[ $lowercase_key != "none" ]]; then
	# run-shell -C runs display-popup inside the tmux server, which is the only
	# way to get #{pane_id} and #{pane_current_path} expanded here: display-popup
	# does not expand formats in its own command arguments. Passing the command
	# as separate argv elements makes tmux execvp it directly, so no shell is
	# forked either. Between them this replaces the old
	# run-shell -> /bin/sh -> scripts/open.sh -> tmux popup -> sh chain.
	#
	# NOTE: dropping scripts/open.sh also drops its `while (( rc == 129 ))` loop.
	# A popup closed from outside (tmux display-popup -C) returns 129 and used to
	# be relaunched; now it stays closed. Escape and ctrl-c are unaffected --
	# tmux forwards those to fzf, which exits 0.
	#
	# The popup no longer inherits the trigger pane's directory, so
	# #{pane_current_path} is passed as the third argument instead.
	#
	# -S -m rather than a script path: a file run as __main__ recompiles the
	# module on every launch, while -m reuses the cached bytecode.
	tmux bind-key "${extrakto_key}" run-shell -C \
		"display-popup -B -w 60% -h 60% -x C -y C \
		-e PYTHONPATH=$script_dir \
		-E $python_bin -S -m extrakto_plugin \
		'#{pane_id}' popup '#{pane_current_path}'"
fi
