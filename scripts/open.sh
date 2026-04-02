#!/bin/sh

script_dir=$(dirname "$0")
script_dir=$(
	cd "$script_dir"
	pwd
)
python_bin="$HOME/.pyenv/versions/anaconda3-2023.09-0/bin/python3"
extrakto="$python_bin $script_dir/../extrakto_plugin.py"

pane_id=$1

# Batch-read all options in a single tmux call
opts=$(tmux display-message -p '#{@extrakto_split_direction}|#{@extrakto_popup_size}|#{@extrakto_popup_position}|#{@extrakto_split_size}')
split_direction="${opts%%|*}"; opts="${opts#*|}"
popup_size="${opts%%|*}"; opts="${opts#*|}"
popup_position="${opts%%|*}"; opts="${opts#*|}"
split_size="$opts"

split_direction="${split_direction:-a}"
popup_size="${popup_size:-90%}"
popup_position="${popup_position:-C}"
split_size="${split_size:-7}"

if [ "$split_direction" = "a" ]; then
	split_direction="p"
fi

extra_options=""
if [ -n "$2" ]; then
	extra_options="-e extrakto_inital_mode=$2"
fi

if [ "$split_direction" = "p" ]; then
	popup_width=$(echo $popup_size | cut -d',' -f1)
	popup_height=$(echo $popup_size | cut -d',' -f2)

	popup_x=$(echo $popup_position | cut -d',' -f1)
	popup_y=$(echo $popup_position | cut -d',' -f2)

	rc=129
	while [ $rc -eq 129 ]; do
		tmux popup \
			-B \
			-w "${popup_width}" \
			-h "${popup_height:-${popup_width}}" \
			-x "${popup_x}" \
			-y "${popup_y:-$popup_x}" \
			$extra_options \
			-E "${extrakto} ${pane_id} popup"
		rc=$?
	done
	exit $rc
else
	tmux split-window \
		-${split_direction} \
		$extra_options \
		-l ${split_size} "tmux setw remain-on-exit off; ${extrakto} ${pane_id} split"
fi
