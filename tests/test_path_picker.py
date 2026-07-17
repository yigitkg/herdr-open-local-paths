import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import local_path_actions as lpa
import path_picker


class PathPickerTests(unittest.TestCase):
    def choice(self, path="/tmp/report.txt"):
        return lpa.PickerChoice(
            candidate=lpa.PathCandidate(path, 10, 2, 4, f"created {path}"),
            resolved=lpa.ResolvedPath(
                source="recent_pane_output",
                original_text=path,
                stripped_text=path,
                path_text=path,
                local_path=path,
                path_kind="posix",
                host_platform="linux",
                exists=True,
                is_file=True,
                parent_exists=True,
            ),
        )

    def test_snapshot_round_trip_is_nonce_scoped_and_deleted_after_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}, clear=True):
                snapshot = lpa.write_picker_snapshot([self.choice()], "open")
                self.assertTrue(snapshot.exists())
                self.assertTrue(snapshot.name.startswith(lpa.PICKER_SNAPSHOT_PREFIX))
                with mock.patch.dict(
                    os.environ,
                    {"HERDR_PLUGIN_STATE_DIR": tmp, "LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT": str(snapshot)},
                    clear=True,
                ):
                    operation, choices = path_picker.load_picker_snapshot()
            self.assertEqual(operation, "open")
            self.assertEqual(choices[0].resolved.local_path, "/tmp/report.txt")
            self.assertFalse(snapshot.exists())

    def test_snapshot_outside_plugin_state_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as state_tmp, tempfile.TemporaryDirectory() as other_tmp:
            snapshot = Path(other_tmp) / "path-picker-unsafe.json"
            snapshot.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_STATE_DIR": state_tmp,
                    "LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT": str(snapshot),
                },
                clear=True,
            ):
                with self.assertRaises(lpa.LocalPathError):
                    path_picker.load_picker_snapshot()

    def test_oversized_snapshot_is_rejected_and_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / f"{lpa.PICKER_SNAPSHOT_PREFIX}large.json"
            snapshot.write_text(" " * (path_picker.MAX_SNAPSHOT_BYTES + 1), encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"HERDR_PLUGIN_STATE_DIR": tmp, "LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT": str(snapshot)},
                clear=True,
            ):
                with self.assertRaisesRegex(lpa.LocalPathError, "too large"):
                    path_picker.load_picker_snapshot()

    def test_decode_key_supports_picker_controls(self):
        self.assertEqual(path_picker.decode_key("\n"), "enter")
        self.assertEqual(path_picker.decode_key("\x1b"), "escape")
        self.assertEqual(path_picker.decode_key("j"), "j")
        self.assertEqual(path_picker.decode_key("k"), "k")
        self.assertEqual(path_picker.decode_key("q"), "q")
        self.assertEqual(path_picker.decode_key("o"), "unknown")
        self.assertEqual(path_picker.decode_key("x"), "unknown")

    def test_decode_kitty_keyboard_sequences_supports_picker_controls(self):
        self.assertEqual(path_picker.decode_escape_sequence("[106u"), "j")
        self.assertEqual(path_picker.decode_escape_sequence("[107;1u"), "k")
        self.assertEqual(path_picker.decode_escape_sequence("[13u"), "enter")
        self.assertEqual(path_picker.decode_escape_sequence("[A"), "up")
        self.assertEqual(path_picker.decode_escape_sequence("[B"), "down")

    def test_clip_line_preserves_width(self):
        self.assertEqual(path_picker.clip_line("short", 10), "short")
        self.assertEqual(len(path_picker.clip_line("a very long path", 8)), 8)

    def test_clip_middle_preserves_both_ends(self):
        self.assertEqual(path_picker.clip_middle("C:/long/folder/name", 9), "C:/l…name")

    def test_operation_labels_are_plain_language(self):
        self.assertEqual(path_picker.operation_label("open"), "open the selected item")
        self.assertEqual(
            path_picker.operation_label("reveal"),
            "show the selected item in its folder",
        )
        self.assertEqual(path_picker.operation_label("copy"), "copy the selected path")

    def test_folder_rows_show_the_complete_path(self):
        choice = self.choice("/tmp/reports")
        choice.resolved.is_file = False
        choice.resolved.is_dir = True
        with (
            mock.patch.object(
                path_picker.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((120, 20)),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            path_picker.render([choice], 0, "open", "")
        self.assertIn("[Folder] /tmp/reports", stdout.getvalue())

    def test_file_rows_show_the_complete_path(self):
        choice = self.choice("/tmp/reports/final.txt")
        with (
            mock.patch.object(
                path_picker.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((120, 20)),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            path_picker.render([choice], 0, "open", "")
        self.assertIn("[File] /tmp/reports/final.txt", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
