#!/usr/bin/env python3

import os
import subprocess
import sys
import traceback

from collections import OrderedDict

from extrakto import Extrakto, get_lines

COLORS = {
    "RED": "\033[0;31m",
    "GREEN": "\033[0;32m",
    "BLUE": "\033[0;34m",
    "PURPLE": "\033[0;35m",
    "CYAN": "\033[0;36m",
    "WHITE": "\033[0;37m",
    "YELLOW": "\033[0;33m",
    "OFF": "\033[0m",
    "BOLD": "\033[1m",
}

DEFAULT_OPTIONS = {
    "@extrakto_clip_tool": "auto",
    "@extrakto_clip_mode": "bg",
    "@extrakto_clip_mode_order": "bg buffer",
    "@extrakto_clip_mode_key": "ctrl-t",
    "@extrakto_copy_key": "enter",
    "@extrakto_edit_key": "ctrl-e",
    "@extrakto_filter_key": "ctrl-f",
    "@extrakto_filter_order": "word all line",
    "@extrakto_fzf_header": "i c o e q s p l f g",
    "@extrakto_path_key": "ctrl-p",
    "@extrakto_fzf_layout": "default",
    "@extrakto_fzf_tool": "fzf",
    "@extrakto_fzf_unset_default_opts": "true",
    "@extrakto_grab_area": "window full",
    "@extrakto_grab_key": "ctrl-g",
    "@extrakto_help_key": "",
    "@extrakto_history_limit": "2000",
    "@extrakto_insert_key": "tab",
    "@extrakto_line_key": "ctrl-l",
    "@extrakto_open_key": "ctrl-o",
    "@extrakto_open_tool": "auto",
    "@extrakto_quote_key": "ctrl-q",
    "@extrakto_squote_key": "ctrl-s",
    "@extrakto_alt": "all",
    "@extrakto_prefix_name": "all",
}


def get_all_extrakto_options():
    """Batch-read all @extrakto_* tmux options in a single subprocess call."""
    raw = subprocess.check_output(
        ["tmux", "show-options", "-g"],
        universal_newlines=True,
    )
    options = {}
    for line in raw.splitlines():
        if line.startswith("@extrakto_"):
            key, _, value = line.partition(" ")
            # tmux may quote values with spaces
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            options[key] = value
    return options


_tmux_options = None


def get_option_only(option):
    global _tmux_options
    if _tmux_options is None:
        _tmux_options = get_all_extrakto_options()
    return _tmux_options.get(option, "")


def get_option(option):
    option_value = get_option_only(option)
    if option_value:
        return option_value
    return DEFAULT_OPTIONS[option] if option in DEFAULT_OPTIONS else ""


def fzf_sel(command, data):
    p = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
    )
    p.stdin.write(data.encode("utf-8") + b"\n")
    p.stdin.close()
    p.wait()
    res = p.stdout.read().decode("utf-8").split("\n")
    # omit last empty line
    return res[:-1]


def get_cap(sel_filter, data, *, extrakto_all, extrakto_any):

    res = []
    run_list = []

    if sel_filter == "all":
        run_list = extrakto_all.all()
        extrakto = extrakto_all
    elif sel_filter == "line":
        res += get_lines(data)
        extrakto = None
    else:
        run_list = [sel_filter]
        extrakto = extrakto_any

    for name in run_list:
        res += extrakto[name].filter(data)

    if not res:
        res = ["NO MATCH - use a different filter"]

    res.reverse()
    return "\n".join([s for s in OrderedDict.fromkeys(res)])


class ExtraktoPlugin:

    def __init__(self, trigger_pane, launch_mode):
        self.trigger_pane = trigger_pane
        self.launch_mode = launch_mode
        # options; note some of the values can be overwritten by capture()
        self.clip_tool = get_option("@extrakto_clip_tool")
        self.clip_mode = get_option_only("@extrakto_clip_tool_run")  # legacy option
        if not self.clip_mode:
            self.clip_mode = get_option("@extrakto_clip_mode")
        self.clip_mode_key = get_option("@extrakto_clip_mode_key")
        self.copy_key = get_option("@extrakto_copy_key")
        self.edit_key = get_option("@extrakto_edit_key")
        self.editor = get_option("@extrakto_editor")
        self.filter_key = get_option("@extrakto_filter_key")
        self.fzf_header = get_option("@extrakto_fzf_header")
        self.fzf_layout = get_option("@extrakto_fzf_layout")
        self.fzf_tool = get_option("@extrakto_fzf_tool")
        self.grab_area = get_option("@extrakto_grab_area")
        self.grab_key = get_option("@extrakto_grab_key")
        self.insert_key = get_option("@extrakto_insert_key")
        self.line_key = get_option("@extrakto_line_key")
        self.open_key = get_option("@extrakto_open_key")
        self.open_tool = get_option("@extrakto_open_tool")
        self.path_key = get_option("@extrakto_path_key")
        self.quote_key = get_option("@extrakto_quote_key")
        self.squote_key = get_option("@extrakto_squote_key")
        self.alt = get_option("@extrakto_alt")
        self.prefix_name = get_option("@extrakto_prefix_name")

        # pre-create Extrakto instances so get_cap doesn't re-parse config each time
        self.extrakto_all = Extrakto(
            alt=(self.alt != "none"),
            prefix_name=(self.prefix_name != "none"),
        )
        self.extrakto_any = Extrakto(
            alt=(self.alt == "any"),
            prefix_name=(self.prefix_name == "any"),
        )

        self.original_grab_area = self.grab_area

        filter_order = get_option("@extrakto_filter_order").split(" ")
        self.next_filter = self.prep_cycle(filter_order)
        # get initial mode passed from cli
        self.next_filter["initial"] = (
            os.environ.get("extrakto_inital_mode", "").strip() or filter_order[0]
        )

        # clip mode order (for cycling with clip_mode_key)
        clip_mode_order = get_option("@extrakto_clip_mode_order").split(" ")
        self.next_clip_mode = self.prep_cycle(clip_mode_order)
        if self.clip_mode not in self.next_clip_mode:
            self.next_clip_mode[self.clip_mode] = clip_mode_order[0]

        # avoid side effects from FZF_DEFAULT_OPTS
        if get_option("@extrakto_fzf_unset_default_opts") == "true":
            os.environ.pop("FZF_DEFAULT_OPTS", None)
            os.environ.pop("FZF_DEFAULT_OPTS_FILE", None)

        if self.clip_tool == "auto":
            self.clip_tool = "pbcopy"

        if self.open_tool == "auto":
            self.open_tool = "open"

        if not self.editor:
            self.editor = os.environ.get("EDITOR", "vi")

        if launch_mode != "popup":
            # check terminal size, zoom pane if too small
            lines = os.get_terminal_size().lines
            if lines < 7:
                subprocess.run("tmux resize-pane -Z", shell=True)

    def prep_cycle(self, keys):
        res = {}
        l = len(keys)
        for i in range(l):
            res[keys[i]] = keys[(i + 1) % l]
        return res

    def copy(self, text):
        if self.clip_mode == "fg":
            # run in foreground as OSC-52 copying won't work otherwise
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
            subprocess.run(
                ["tmux", "run-shell", f"tmux show-buffer|{self.clip_tool}"], check=True
            )
        elif self.clip_mode == "tmux_osc52":
            # use native tmux 3.2 OSC 52 functionality
            subprocess.run(["tmux", "set-buffer", "-w", "--", text], check=True)
        elif self.clip_mode == "buffer":
            # only save to tmux buffer, no clipboard
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
        else:
            # run in background as xclip won't work otherwise
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
            subprocess.run(
                ["tmux", "run-shell", "-b", f"tmux show-buffer|{self.clip_tool}"],
                check=True,
            )

    def open(self, path):
        if self.open_tool:
            subprocess.run(
                ["tmux", "run-shell", "-b", f"cd -- $PWD; {self.open_tool} {path}"],
                check=True,
            )

    # this returns the start point parameter for `tmux capture-pane`.
    def get_capture_pane_start(self):
        area = self.grab_area
        # strip scope prefix to get the size part
        for prefix in ("all ", "session ", "window "):
            if area.startswith(prefix):
                area = area[len(prefix):]
                break

        if area == "recent":
            capture_start = "-10"
        elif area == "full":
            history_limit = get_option("@extrakto_history_limit")
            capture_start = f"-{history_limit}"
        else:
            capture_start = f"-{area}"
        return capture_start

    def capture_panes(self):
        captured = ""
        capture_pane_start = self.get_capture_pane_start()

        if self.grab_area.startswith("all "):
            # all panes in all sessions
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                universal_newlines=True,
            ).strip().split("\n")
            for pane_id in panes:
                if pane_id and pane_id != self.trigger_pane:
                    captured += self.capture_pane(pane_id, capture_pane_start) + "\n"
        elif self.grab_area.startswith("session "):
            # all panes in all windows of the current session
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-s", "-F", "#{pane_id}"],
                universal_newlines=True,
            ).strip().split("\n")
            for pane_id in panes:
                if pane_id and pane_id != self.trigger_pane:
                    captured += self.capture_pane(pane_id, capture_pane_start) + "\n"
        elif self.grab_area.startswith("window "):
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-F", "#{pane_active}:#{pane_id}"],
                universal_newlines=True,
            ).split("\n")
            for pane in panes:
                # exclude the active (for split) and trigger panes
                # in popup mode the active and tigger panes are the same
                # todo: split by :
                if pane.startswith("0:") and pane[:2] != self.trigger_pane:
                    captured += self.capture_pane(pane[2:], capture_pane_start) + "\n"

        captured += self.capture_pane(self.trigger_pane, capture_pane_start)
        return captured

    def capture_pane(self, pane, capture_pane_start):
        command = ["tmux", "capture-pane", "-pJ", "-S", capture_pane_start, "-t", pane]

        if self.grab_area.endswith("recent"):
            try:
                pane_in_mode, scroll_position, pane_height = [
                    int(n)
                    for n in subprocess.check_output(
                        [
                            "tmux",
                            "display-message",
                            "-p",
                            "-t",
                            pane,
                            "#{pane_in_mode}\t#{scroll_position}\t#{pane_height}",
                        ],
                        universal_newlines=True,
                        encoding="utf-8",
                    )
                    .strip()
                    .split("\t")
                ]

                if pane_in_mode == 1:
                    # In copy-mode, "recent" should follow the currently visible viewport.
                    start = int(capture_pane_start) - scroll_position
                    end = (pane_height - 1) - scroll_position
                    command = [
                        "tmux",
                        "capture-pane",
                        "-pJ",
                        "-S",
                        str(start),
                        "-E",
                        str(end),
                        "-t",
                        pane,
                    ]
            except (ValueError, subprocess.CalledProcessError):
                # If formats are unavailable, fall back to regular recent capture.
                pass

        return subprocess.check_output(
            command,
            universal_newlines=True,
            encoding="utf-8",
        )

    def has_single_pane(self):
        num_panes = len(
            subprocess.check_output(
                ["tmux", "list-panes"], universal_newlines=True
            ).split("\n")
        )
        if self.launch_mode == "popup":
            return num_panes == 1
        else:
            return num_panes == 2

    def capture(self):
        sel_filter = self.next_filter["initial"]
        header_parts = []
        for o in self.fzf_header.split(" "):
            if not o:
                continue
            if o == "i":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.insert_key}{COLORS['OFF']}=insert"
                )
            elif o == "c":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.copy_key}{COLORS['OFF']}=copy"
                )
            elif o == "o":
                if self.open_tool:
                    header_parts.append(
                        f"{COLORS['BOLD']}{self.open_key}{COLORS['OFF']}=open"
                    )
            elif o == "e":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.edit_key}{COLORS['OFF']}=edit"
                )
            elif o == "q":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.quote_key}{COLORS['OFF']}=quote"
                )
            elif o == "s":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.squote_key}{COLORS['OFF']}=squote"
                )
            elif o == "p":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.path_key}{COLORS['OFF']}=path"
                )
            elif o == "l":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.line_key}{COLORS['OFF']}=line"
                )
            elif o == "f":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.filter_key}{COLORS['OFF']}=filter [{COLORS['YELLOW']}{COLORS['BOLD']}:filter:{COLORS['OFF']}]"
                )
            elif o == "g":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.grab_key}{COLORS['OFF']}=grab [{COLORS['YELLOW']}{COLORS['BOLD']}:ga:{COLORS['OFF']}]"
                )
            elif o == "m":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.clip_mode_key}{COLORS['OFF']}=clip [{COLORS['YELLOW']}{COLORS['BOLD']}:clip_mode:{COLORS['OFF']}]"
                )
            elif o == "h":
                continue
            else:
                header_parts.append("(config error)")

        header_tmpl = ", ".join(header_parts)
        expect_keys = list(
            OrderedDict.fromkeys(
                key
                for key in [
                    "ctrl-c",
                    "ctrl-g",
                    "esc",
                    self.insert_key,
                    self.copy_key,
                    self.filter_key,
                    self.edit_key,
                    self.quote_key,
                    self.squote_key,
                    self.path_key,
                    self.line_key,
                    self.open_key,
                    self.grab_key,
                    self.clip_mode_key,
                ]
                if key
            )
        )

        query = ""
        while True:
            header = (
                header_tmpl.replace(":ga:", self.grab_area)
                .replace(":filter:", sel_filter)
                .replace(":clip_mode:", self.clip_mode)
                .replace("ctrl-", "^")
            )

            # for troubleshooting add `tee /tmp/stageN | ` between the commands
            try:
                fzf_cmd = [
                    self.fzf_tool,
                    "--multi",
                    "--print-query",
                    f"--query={query}",
                    f"--header={header}",
                    f"--expect={','.join(expect_keys)}",
                    "--tiebreak=index",
                    f"--layout={self.fzf_layout}",
                    "--no-info",
                ]
                query, key, *selection = fzf_sel(
                    fzf_cmd,
                    get_cap(sel_filter, self.capture_panes(),
                           extrakto_all=self.extrakto_all,
                           extrakto_any=self.extrakto_any),
                )
            except Exception as e:
                msg = (
                    str(fzf_cmd)
                    + "\n"
                    + traceback.format_exc()
                    + "\n"
                    + "error: unable to extract - check/report errors above"
                    + "\n"
                    + "If fzf is not found you need to set the fzf path in options (see readme)."
                )
                print(msg)
                confirm = input("Copy this message to the clipboard? [Y/n]")
                if confirm != "n":
                    self.copy(msg)
                sys.exit(0)

            # selection will be without or with the filter name prefixing the entry
            # "example quoted text here"
            # quote: "example quoted text here"
            text = ""
            if (
                self.prefix_name == "all" and sel_filter == "all"
            ) or self.prefix_name == "any":
                selection = [next(iter(s.split(": ", 1)[1:2]), s) for s in selection]

            if sel_filter in ("all", "line"):
                text = "\n".join(selection)
            else:
                text = " ".join(selection)

            if key == self.copy_key:
                self.copy(text)
                return 0
            elif key == self.insert_key:
                subprocess.run(["tmux", "set-buffer", "--", text], check=True)
                subprocess.run(
                    ["tmux", "paste-buffer", "-p", "-t", self.trigger_pane], check=True
                )
                return 0
            elif key == self.filter_key:
                sel_filter = self.next_filter[sel_filter]
            elif key == self.quote_key:
                sel_filter = "quote"
            elif key == self.squote_key:
                sel_filter = "s-quote"
            elif key == self.path_key:
                sel_filter = "path"
            elif key == self.line_key:
                sel_filter = "line"
            elif key == self.clip_mode_key:
                self.clip_mode = self.next_clip_mode[self.clip_mode]
            elif key == self.grab_key:
                # cycle: recent -> window recent -> session recent -> all recent
                #     -> full -> window full -> session full -> all full
                #     -> custom (if any) -> recent ...
                grab_cycle = ["recent"]
                if not self.has_single_pane():
                    grab_cycle.append("window recent")
                grab_cycle.extend(["session recent", "all recent", "full"])
                if not self.has_single_pane():
                    grab_cycle.append("window full")
                grab_cycle.extend(["session full", "all full"])
                # append custom grab area if it's not one of the standard ones
                if not self.original_grab_area.startswith(
                    ("window ", "session ", "all ", "recent", "full")
                ):
                    grab_cycle.append(self.original_grab_area)

                try:
                    idx = grab_cycle.index(self.grab_area)
                    self.grab_area = grab_cycle[(idx + 1) % len(grab_cycle)]
                except ValueError:
                    self.grab_area = "recent"
            elif key == self.open_key:
                self.open(text)
                return 0
            elif key == self.edit_key:
                subprocess.run(
                    [
                        "tmux",
                        "if-shell",
                        "-t",
                        self.trigger_pane,
                        "-F",
                        "#{pane_in_mode}",
                        f"send-keys -t {self.trigger_pane} -X cancel",
                        ";",
                        "send-keys",
                        "-t",
                        self.trigger_pane,
                        f"{self.editor} -- {text}",
                        "C-m",
                    ],
                    check=True,
                )
                return 0
            else:
                return 0

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: extrakto-plugin.py trigger_pane launch_mode")
        sys.exit(1)
    else:
        ExtraktoPlugin(sys.argv[1], sys.argv[2]).capture()
