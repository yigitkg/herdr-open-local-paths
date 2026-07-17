import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import local_path_actions as lpa


class LocalPathActionsTests(unittest.TestCase):
    def resolve(self, text, cwd=None):
        ctx = lpa.PluginContext(selected_text=text, focused_pane_cwd=cwd)
        with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
            return lpa.resolve_from_context(ctx)

    def test_windows_drive_path(self):
        resolved = self.resolve(r"C:\Users\me\Downloads\report.xlsx")
        self.assertEqual(resolved.local_path, r"C:\Users\me\Downloads\report.xlsx")
        self.assertEqual(resolved.path_kind, "windows-drive")

    def test_file_url_windows(self):
        ctx = lpa.PluginContext(clicked_url="file:///C:/Users/me/Desktop/a%20b.xlsx")
        resolved = lpa.resolve_from_context(ctx)
        self.assertEqual(resolved.local_path, r"C:\Users\me\Desktop\a b.xlsx")

    def test_file_url_posix(self):
        ctx = lpa.PluginContext(clicked_url="file:///tmp/a%20b.txt")
        with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
            resolved = lpa.resolve_from_context(ctx)
        self.assertEqual(resolved.local_path, "/tmp/a b.txt")

    def test_file_url_localhost_is_local(self):
        self.assertEqual(lpa.file_url_to_path("file://localhost/tmp/report.txt"), "/tmp/report.txt")

    def test_file_url_rejects_decoded_control_characters(self):
        for encoded in ("%00", "%0A", "%1B"):
            with self.assertRaises(lpa.LocalPathError, msg=encoded):
                lpa.file_url_to_path(f"file:///tmp/a{encoded}b.txt")

    def test_empty_file_url_is_rejected(self):
        with self.assertRaises(lpa.LocalPathError):
            lpa.file_url_to_path("file://")

    def test_relative_path_requires_cwd(self):
        with self.assertRaises(lpa.LocalPathError):
            self.resolve("./report.csv")

    def test_relative_path_resolves_against_focused_pane_cwd(self):
        resolved = self.resolve("./report.csv", "/tmp/project")
        self.assertEqual(resolved.local_path, "/tmp/project/report.csv")

    def test_line_suffix_is_removed(self):
        resolved = self.resolve("/tmp/project/src/app.py:12:4")
        self.assertEqual(resolved.local_path, "/tmp/project/src/app.py")
        self.assertEqual(resolved.line, 12)
        self.assertEqual(resolved.column, 4)

    def test_quoted_path_is_unwrapped(self):
        resolved = self.resolve('"/tmp/report.csv"')
        self.assertEqual(resolved.local_path, "/tmp/report.csv")

    def test_nul_rejected(self):
        with self.assertRaises(lpa.LocalPathError):
            self.resolve("/tmp/a\x00b")

    def test_shell_metacharacters_are_literal_path_text(self):
        resolved = self.resolve(r"C:\tmp\a & calc.exe.txt")
        self.assertEqual(resolved.local_path, r"C:\tmp\a & calc.exe.txt")

    def test_percent_encoded_semicolon_is_literal(self):
        ctx = lpa.PluginContext(clicked_url="file:///tmp/a%3Btouch%20hacked.txt")
        with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
            resolved = lpa.resolve_from_context(ctx)
        self.assertEqual(resolved.local_path, "/tmp/a;touch hacked.txt")

    def test_empty_text_rejected(self):
        ctx = lpa.PluginContext(selected_text="   ")
        with self.assertRaises(lpa.LocalPathError):
            lpa.resolve_from_context(ctx)

    def test_unsupported_url_rejected(self):
        ctx = lpa.PluginContext(clicked_url="https://example.com/file.txt")
        with self.assertRaises(lpa.LocalPathError):
            with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
                lpa.resolve_from_context(ctx)

    def test_high_risk_extension_rejected_without_override(self):
        with tempfile.NamedTemporaryFile(suffix=".exe") as f:
            resolved = self.resolve(f.name)
            with self.assertRaises(lpa.LocalPathError):
                lpa.ensure_open_allowed(resolved)

    def test_extensionless_posix_executable_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "tool"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            resolved = self.resolve(str(executable))
            with self.assertRaisesRegex(lpa.LocalPathError, "Executable files"):
                lpa.ensure_open_allowed(resolved)

    def test_open_command_windows_from_wsl(self):
        with mock.patch.object(lpa, "is_wsl_environment", return_value=True):
            exe, args = lpa.open_command(r"C:\Users\me\file.xlsx", "linux")
        self.assertEqual(exe, "explorer.exe")
        self.assertEqual(args, [r"C:\Users\me\file.xlsx"])

    def test_open_command_windows_native_uses_startfile_sentinel(self):
        with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
            exe, args = lpa.open_command(r"C:\Users\me\file.xlsx", "windows")
        self.assertEqual(exe, "os.startfile")
        self.assertEqual(args, [r"C:\Users\me\file.xlsx"])

    def test_reveal_command_linux_falls_back_to_parent(self):
        exe, args = lpa.reveal_command("/tmp/project/file.txt", "linux", False)
        self.assertEqual(exe, "xdg-open")
        self.assertEqual(args, ["/tmp/project"])

    def test_explorer_command_waits_for_windows_to_accept_request(self):
        with (
            mock.patch.object(lpa.shutil, "which", return_value="/mnt/c/Windows/explorer.exe"),
            mock.patch.object(
                lpa.subprocess,
                "run",
                return_value=mock.Mock(returncode=0),
            ) as run,
        ):
            lpa.run_command("explorer.exe", [r"/select,C:\tmp\report.txt"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_explorer_command_reports_rejected_request(self):
        with (
            mock.patch.object(lpa.shutil, "which", return_value="/mnt/c/Windows/explorer.exe"),
            mock.patch.object(
                lpa.subprocess,
                "run",
                return_value=mock.Mock(returncode=2),
            ),
        ):
            with self.assertRaisesRegex(lpa.LocalPathError, "exit code 2"):
                lpa.run_command("explorer.exe", [r"C:\tmp\report.txt"])

    def test_explorer_accepts_wsl_shell_handoff_exit_code(self):
        with (
            mock.patch.object(lpa.shutil, "which", return_value="/mnt/c/Windows/explorer.exe"),
            mock.patch.object(
                lpa.subprocess,
                "run",
                return_value=mock.Mock(returncode=1),
            ),
        ):
            lpa.run_command("explorer.exe", [r"/select,C:\tmp\report.txt"])

    def test_diagnose_context_from_env(self):
        raw = {"selected_text": "/tmp/file.txt", "focused_pane_cwd": "/tmp"}
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(raw)}, clear=True):
            ctx = lpa.load_context()
        self.assertEqual(ctx.selected_text, "/tmp/file.txt")
        self.assertEqual(ctx.focused_pane_cwd, "/tmp")

    def test_extract_path_candidates_from_recent_output(self):
        output = "\n".join(
            [
                "saved C:\\Users\\me\\Downloads\\report.xlsx",
                "also /tmp/project/src/app.py:12:4",
                "relative src/main.rs:8",
                "url file:///C:/Users/me/Desktop/a%20b.xlsx",
            ]
        )
        candidates = lpa.extract_path_candidates(output)
        self.assertIn(r"C:\Users\me\Downloads\report.xlsx", candidates)
        self.assertIn("/tmp/project/src/app.py:12:4", candidates)
        self.assertIn("src/main.rs:8", candidates)
        self.assertIn("file:///C:/Users/me/Desktop/a%20b.xlsx", candidates)

    def test_extract_path_candidates_ignores_powershell_prompt_cwd(self):
        output = "\n".join(
            [
                r"PS C:\work\project>",
                r"PS C:\work\project> Write-Output 'C:\work\project\README.md'",
                r"C:\work\project\README.md",
                r"PS C:\work\project>",
            ]
        )
        self.assertEqual(
            lpa.extract_path_candidates(output),
            [r"C:\work\project\README.md"],
        )

    def test_extract_quoted_and_markdown_paths_with_spaces(self):
        output = "\n".join(
            [
                'created "/tmp/My Reports/final report.xlsx"',
                r"saved `C:\Users\me\My Reports\final report.xlsx`",
                "edited 'docs/release notes.md:12'",
            ]
        )
        candidates = lpa.extract_path_candidates(output)
        self.assertEqual(
            candidates,
            [
                "/tmp/My Reports/final report.xlsx",
                r"C:\Users\me\My Reports\final report.xlsx",
                "docs/release notes.md:12",
            ],
        )

    def test_extract_markdown_link_destination_with_spaces(self):
        self.assertEqual(
            lpa.extract_path_candidates("Generated [report](</tmp/My Reports/final report.xlsx>)"),
            ["/tmp/My Reports/final report.xlsx"],
        )

    def test_strip_ansi_removes_osc_links_and_normalizes_carriage_returns(self):
        output = "\x1b]8;;file:///tmp/report.csv\x1b\\/tmp/report.csv\x1b]8;;\x1b\\\r/tmp/other.csv"
        self.assertEqual(
            lpa.extract_path_candidates(output),
            ["/tmp/report.csv", "/tmp/other.csv"],
        )

    def test_extract_candidates_globally_deduplicates_and_keeps_newest_occurrence(self):
        output = "\n".join(
            [
                "first /tmp/report.csv:2",
                "another /tmp/other.csv",
                "latest `/tmp/report.csv:99`",
            ]
        )
        records = lpa.extract_path_candidate_records(output)
        self.assertEqual([record.text for record in records], ["/tmp/other.csv", "/tmp/report.csv:99"])
        self.assertEqual(records[-1].line, 3)
        self.assertEqual(records[-1].column, 9)
        self.assertEqual(records[-1].excerpt, "latest `/tmp/report.csv:99`")

    def test_file_url_does_not_also_emit_nested_posix_path(self):
        candidates = lpa.extract_path_candidates("open file:///tmp/report.csv")
        self.assertEqual(candidates, ["file:///tmp/report.csv"])

    def test_bare_slashes_are_not_picker_candidates(self):
        self.assertEqual(lpa.extract_path_candidates("operator // and root /"), [])

    def test_plain_path_and_file_url_share_normalized_identity(self):
        candidates = lpa.extract_path_candidates("old /tmp/report.csv\nnew file:///tmp/report.csv")
        self.assertEqual(candidates, ["file:///tmp/report.csv"])

    def test_resolve_latest_path_uses_newest_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "older.txt"
            newer = root / "newer.txt"
            older.write_text("old")
            newer.write_text("new")
            output = f"first {older}\nmissing {root / 'missing.txt'}\nlast {newer}\n"
            ctx = lpa.PluginContext(focused_pane_id="w1:p1", focused_pane_cwd=tmp)
            with (
                mock.patch.object(lpa, "read_recent_pane_output", return_value=output),
                mock.patch.object(lpa, "is_wsl_environment", return_value=False),
            ):
                resolved = lpa.resolve_latest_path_from_pane(ctx)
            self.assertEqual(resolved.local_path, str(newer))

    def test_resolve_latest_path_prefers_file_over_newer_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "report.csv"
            target.write_text("ok")
            output = f"file {target}\nnewer directory {root}\n"
            ctx = lpa.PluginContext(focused_pane_id="w1:p1", focused_pane_cwd=tmp)
            with (
                mock.patch.object(lpa, "read_recent_pane_output", return_value=output),
                mock.patch.object(lpa, "is_wsl_environment", return_value=False),
            ):
                resolved = lpa.resolve_latest_path_from_pane(ctx)
            self.assertEqual(resolved.local_path, str(target))

    def test_resolve_latest_path_supports_relative_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            target = src / "main.py"
            target.write_text("print('ok')")
            output = "edited src/main.py:10"
            ctx = lpa.PluginContext(focused_pane_id="w1:p1", focused_pane_cwd=tmp)
            with (
                mock.patch.object(lpa, "read_recent_pane_output", return_value=output),
                mock.patch.object(lpa, "is_wsl_environment", return_value=False),
            ):
                resolved = lpa.resolve_latest_path_from_pane(ctx)
            self.assertEqual(resolved.local_path, str(target))
            self.assertEqual(resolved.line, 10)

    def test_diagnose_latest_path_reports_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.csv"
            target.write_text("ok")
            ctx = lpa.PluginContext(focused_pane_id="w1:p1", focused_pane_cwd=tmp)
            with (
                mock.patch.object(lpa, "read_recent_pane_output", return_value=f"created {target}\n"),
                mock.patch.object(lpa, "is_wsl_environment", return_value=False),
            ):
                report = lpa.diagnose_latest_path_from_pane(ctx)
            self.assertEqual(report["candidate_count"], 1)
            self.assertEqual(report["selected"]["local_path"], str(target))

    def test_read_recent_pane_output_decodes_herdr_as_utf8(self):
        context = lpa.PluginContext(focused_pane_id="w1:p1")
        with mock.patch.object(
            lpa.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout="C:\\work\\résumé.txt", stderr=""),
        ) as run:
            output = lpa.read_recent_pane_output(context)
        self.assertEqual(output, "C:\\work\\résumé.txt")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_collect_picker_choices_returns_existing_paths_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first report.txt"
            second = root / "second report.txt"
            first.write_text("first")
            second.write_text("second")
            output = f"created `{first}`\ncreated `{second}`\nagain `{first}`\n"
            context = lpa.PluginContext(focused_pane_cwd=tmp)
            with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
                choices = lpa.collect_picker_choices(context, output)
            self.assertEqual(
                [choice.resolved.local_path for choice in choices],
                [str(first), str(second)],
            )
            self.assertEqual(choices[0].candidate.line, 3)

    def test_collect_picker_choices_lists_files_before_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "report.txt"
            target.write_text("ok")
            output = f"older file {target}\nnewer directory {root}\n"
            context = lpa.PluginContext(focused_pane_cwd=tmp)
            with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
                choices = lpa.collect_picker_choices(context, output)
            self.assertEqual([choice.resolved.local_path for choice in choices], [str(target), str(root)])

    def test_adaptive_action_opens_picker_for_multiple_paths(self):
        resolved = lpa.ResolvedPath(
            source="test",
            original_text="/tmp/a",
            stripped_text="/tmp/a",
            path_text="/tmp/a",
            local_path="/tmp/a",
            path_kind="posix",
            host_platform="linux",
            exists=True,
            is_file=True,
        )
        choices = [
            lpa.PickerChoice(lpa.PathCandidate(f"/tmp/{name}", index, 1, 1, name), resolved)
            for index, name in enumerate(("a", "b"))
        ]
        with (
            mock.patch.object(lpa, "collect_picker_choices", return_value=choices),
            mock.patch.object(lpa, "launch_path_picker") as launch,
        ):
            lpa.run_adaptive_path_action(lpa.PluginContext(), "open")
        launch.assert_called_once_with(choices, "open")

    def test_adaptive_action_performs_directly_for_one_path(self):
        resolved = lpa.ResolvedPath(
            source="test",
            original_text="/tmp/a",
            stripped_text="/tmp/a",
            path_text="/tmp/a",
            local_path="/tmp/a",
            path_kind="posix",
            host_platform="linux",
            exists=True,
            is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("/tmp/a", 0, 1, 1, "/tmp/a"), resolved)
        with (
            mock.patch.object(lpa, "collect_picker_choices", return_value=[choice]),
            mock.patch.object(lpa, "perform_path_operation") as perform,
        ):
            lpa.run_adaptive_path_action(lpa.PluginContext(), "reveal")
        perform.assert_called_once_with(resolved, "reveal")

    def test_remote_pane_refuses_open_even_when_local_path_exists(self):
        resolved = lpa.ResolvedPath(
            source="test",
            original_text="./report.txt",
            stripped_text="./report.txt",
            path_text="./report.txt",
            local_path="/tmp/report.txt",
            path_kind="relative",
            host_platform="linux",
            exists=True,
            is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("./report.txt", 0, 1, 1, "./report.txt"), resolved)
        context = lpa.PluginContext(focused_pane_id="w1:p1")
        with (
            mock.patch.object(lpa, "collect_picker_choices", return_value=[choice]),
            mock.patch.object(lpa, "pane_locality", return_value="remote"),
            mock.patch.object(lpa, "perform_path_operation") as perform,
        ):
            with self.assertRaisesRegex(lpa.LocalPathError, "appears to be remote"):
                lpa.run_adaptive_path_action(context, "open")
        perform.assert_not_called()

    def test_remote_override_is_explicit(self):
        resolved = lpa.ResolvedPath(
            source="test", original_text="./a", stripped_text="./a", path_text="./a",
            local_path="/tmp/a", path_kind="relative", host_platform="linux",
            exists=True, is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("./a", 0, 1, 1, "./a"), resolved)
        with (
            mock.patch.dict(os.environ, {"LOCAL_PATH_ACTIONS_ALLOW_REMOTE": "1"}, clear=True),
            mock.patch.object(lpa, "collect_picker_choices", return_value=[choice]),
            mock.patch.object(lpa, "perform_path_operation") as perform,
        ):
            lpa.run_adaptive_path_action(lpa.PluginContext(), "open")
        perform.assert_called_once_with(resolved, "open")

    def test_pane_locality_detects_ssh_and_container_exec(self):
        context = lpa.PluginContext(focused_pane_id="w1:p1")
        for argv in (["ssh", "server"], ["mosh", "server"], ["docker", "exec", "box", "sh"], ["kubectl", "exec", "pod", "--", "sh"]):
            payload = {"result": {"process_info": {"foreground_processes": [{"argv": argv}]}}}
            with mock.patch.object(
                lpa.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))
            ):
                self.assertEqual(lpa.pane_locality(context), "remote", msg=argv)

    def test_unknown_pane_locality_refuses_relative_candidate(self):
        resolved = lpa.ResolvedPath(
            source="test", original_text="./a", stripped_text="./a", path_text="./a",
            local_path="/tmp/a", path_kind="relative", host_platform="linux",
            exists=True, is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("./a", 0, 1, 1, "./a"), resolved)
        with (
            mock.patch.object(lpa, "collect_picker_choices", return_value=[choice]),
            mock.patch.object(lpa, "pane_locality", return_value="unknown"),
        ):
            with self.assertRaisesRegex(lpa.LocalPathError, "relative paths were refused"):
                lpa.run_adaptive_path_action(lpa.PluginContext(), "open")

    def test_picker_launch_keeps_snapshot_for_successful_popup(self):
        resolved = lpa.ResolvedPath(
            source="test",
            original_text="/tmp/a",
            stripped_text="/tmp/a",
            path_text="/tmp/a",
            local_path="/tmp/a",
            path_kind="posix",
            host_platform="linux",
            exists=True,
            is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("/tmp/a", 0, 1, 1, "/tmp/a"), resolved)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}, clear=True),
                mock.patch.object(
                    lpa.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ) as run,
            ):
                lpa.launch_path_picker([choice], "open")
            snapshots = list(Path(tmp).glob(f"{lpa.PICKER_SNAPSHOT_PREFIX}*.json"))
            self.assertEqual(len(snapshots), 1)
            self.assertIn(f"LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT={snapshots[0]}", run.call_args.args[0])

    def test_picker_launch_removes_snapshot_when_popup_fails(self):
        resolved = lpa.ResolvedPath(
            source="test",
            original_text="/tmp/a",
            stripped_text="/tmp/a",
            path_text="/tmp/a",
            local_path="/tmp/a",
            path_kind="posix",
            host_platform="linux",
            exists=True,
            is_file=True,
        )
        choice = lpa.PickerChoice(lpa.PathCandidate("/tmp/a", 0, 1, 1, "/tmp/a"), resolved)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}, clear=True),
                mock.patch.object(
                    lpa.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=1, stdout="", stderr="ui busy"),
                ),
            ):
                with self.assertRaisesRegex(lpa.LocalPathError, "ui busy"):
                    lpa.launch_path_picker([choice], "open")
            self.assertEqual(list(Path(tmp).glob(f"{lpa.PICKER_SNAPSHOT_PREFIX}*.json")), [])

    def test_stale_picker_snapshots_are_pruned_without_touching_recent_or_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            stale = state_dir / f"{lpa.PICKER_SNAPSHOT_PREFIX}stale.json"
            recent = state_dir / f"{lpa.PICKER_SNAPSHOT_PREFIX}recent.json"
            unrelated = state_dir / "other.json"
            for path in (stale, recent, unrelated):
                path.write_text("{}")
            now = 10_000.0
            os.utime(stale, (now - lpa.PICKER_SNAPSHOT_TTL_SECONDS - 1,) * 2)
            os.utime(recent, (now,) * 2)
            os.utime(unrelated, (now - lpa.PICKER_SNAPSHOT_TTL_SECONDS - 1,) * 2)
            lpa.prune_stale_picker_snapshots(state_dir, now=now)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_scan_line_limit_is_bounded_and_invalid_values_use_default(self):
        self.assertEqual(lpa.scan_line_limit(None), lpa.DEFAULT_SCAN_LINES)
        self.assertEqual(lpa.scan_line_limit("invalid"), lpa.DEFAULT_SCAN_LINES)
        self.assertEqual(lpa.scan_line_limit("1"), lpa.MIN_SCAN_LINES)
        self.assertEqual(lpa.scan_line_limit("999999"), lpa.MAX_SCAN_LINES)

    def test_unc_path_skips_filesystem_inspection(self):
        context = lpa.PluginContext(selected_text=r"\\server\share\report.txt")
        with mock.patch.object(lpa, "inspect_path_for_resolved") as inspect:
            resolved = lpa.resolve_from_context(context)
        inspect.assert_not_called()
        self.assertFalse(resolved.exists)
        self.assertIn("skipped", resolved.warning)

    def test_clipboard_failure_prints_path_and_reports_truthfully(self):
        with (
            mock.patch.object(lpa, "is_wsl_environment", return_value=False),
            mock.patch.object(lpa, "host_platform", return_value="linux"),
            mock.patch.object(lpa.shutil, "which", return_value=None),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            with self.assertRaisesRegex(lpa.LocalPathError, "printed to stdout"):
                lpa.copy_text("/tmp/report.txt")
        self.assertEqual(stdout.getvalue().strip(), "/tmp/report.txt")

    def test_wsl_literal_windows_path_checks_mounted_equivalent(self):
        def inspect(path):
            if path == "/mnt/c/Users/me/report.txt":
                return True, True, False, True
            return False, False, False, False

        context = lpa.PluginContext(selected_text=r"C:\Users\me\report.txt")
        with (
            mock.patch.object(lpa, "is_wsl_environment", return_value=True),
            mock.patch.object(lpa, "inspect_path", side_effect=inspect),
        ):
            resolved = lpa.resolve_from_context(context)
        self.assertTrue(resolved.exists)
        self.assertEqual(resolved.local_path, r"C:\Users\me\report.txt")

    def test_additional_windows_risky_extensions_are_blocked(self):
        for suffix in (".com", ".scr", ".cpl", ".hta", ".reg", ".url", ".wsf"):
            resolved = lpa.ResolvedPath(
                source="test",
                original_text="C:\\tmp\\file" + suffix,
                stripped_text="C:\\tmp\\file" + suffix,
                path_text="C:\\tmp\\file" + suffix,
                local_path="C:\\tmp\\file" + suffix,
                path_kind="windows-drive",
                host_platform="windows",
                exists=True,
                is_file=True,
            )
            with self.assertRaises(lpa.LocalPathError, msg=suffix):
                lpa.ensure_open_allowed(resolved)

    def test_windows_trailing_dot_space_and_ads_paths_are_refused(self):
        for path in (
            "C:\\tmp\\evil.exe.",
            "C:\\tmp\\evil.exe..",
            "C:\\tmp\\evil.CMD...   ",
            "C:\\tmp\\report.txt:payload.exe",
        ):
            with self.assertRaises(lpa.LocalPathError, msg=path):
                lpa.ensure_windows_path_is_canonical(path)

    def test_realistic_generated_file_corpus_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["report 2026.pdf", "data.csv", "dashboard.html", "chart.png", "analysis.py"]
            paths = []
            for name in names:
                path = root / name
                path.write_bytes(b"test")
                paths.append(path)
            output = "\n".join(f"Generated `{path}`" for path in paths)
            with mock.patch.object(lpa, "is_wsl_environment", return_value=False):
                choices = lpa.collect_picker_choices(lpa.PluginContext(focused_pane_cwd=tmp), output)
            self.assertEqual({choice.resolved.local_path for choice in choices}, {str(path) for path in paths})


if __name__ == "__main__":
    unittest.main()
