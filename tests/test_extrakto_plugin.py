#!/usr/bin/env python3

import unittest

from unittest import mock

import extrakto_plugin


class TestExtraktoPlugin(unittest.TestCase):
    def setUp(self):
        extrakto_plugin._tmux_options = None

    def tearDown(self):
        extrakto_plugin._tmux_options = None

    @mock.patch("extrakto_plugin.get_all_extrakto_options", return_value={})
    def test_capture_header_and_expect_use_line_key(self, _get_all_options):
        plugin = extrakto_plugin.ExtraktoPlugin("%1", "popup")
        commands = []

        def fake_fzf_sel(command, _data):
            commands.append(command)
            return ["", plugin.copy_key, "alpha"]

        with mock.patch.object(plugin, "capture_panes", return_value="alpha\nbeta"), \
                mock.patch.object(plugin, "copy"), \
                mock.patch("extrakto_plugin.fzf_sel", side_effect=fake_fzf_sel):
            plugin.capture()

        header_arg = next(arg for arg in commands[0] if arg.startswith("--header="))
        expect_arg = next(arg for arg in commands[0] if arg.startswith("--expect="))

        self.assertIn("=line", header_arg)
        self.assertNotIn("=help", header_arg)
        self.assertIn(plugin.line_key, expect_arg)
        self.assertNotIn("help", expect_arg)

    @mock.patch("extrakto_plugin.get_all_extrakto_options", return_value={})
    def test_line_key_switches_to_line_filter(self, _get_all_options):
        plugin = extrakto_plugin.ExtraktoPlugin("%1", "popup")
        filters = []
        fzf_responses = iter(
            [
                ["", plugin.line_key],
                ["", plugin.copy_key, "full line"],
            ]
        )

        def fake_get_cap(sel_filter, _data, **_kwargs):
            filters.append(sel_filter)
            return "entry"

        with mock.patch.object(plugin, "capture_panes", return_value="alpha\nbeta"), \
                mock.patch.object(plugin, "copy"), \
                mock.patch("extrakto_plugin.get_cap", side_effect=fake_get_cap), \
                mock.patch("extrakto_plugin.fzf_sel", side_effect=lambda *_args: next(fzf_responses)):
            plugin.capture()

        self.assertEqual(filters, ["word", "line"])


if __name__ == "__main__":
    unittest.main()
