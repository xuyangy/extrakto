#!/bin/sh

script_dir=$(dirname "$0")
script_dir=$(
	cd "$script_dir"
	pwd
)
python_bin="$HOME/.pyenv/versions/anaconda3-2023.09-0/bin/python3"
extrakto="$python_bin $script_dir/../extrakto_plugin.py"

pane_id=$1

rc=129
while [ $rc -eq 129 ]; do
	tmux popup \
		-B \
		-w "60%" \
		-h "60%" \
		-x "C" \
		-y "C" \
		-E "${extrakto} ${pane_id} popup"
	rc=$?
done
exit $rc
