#!/usr/bin/env python3
"""Interactive popup picker for Herdr Local Path Actions."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from local_path_actions import (
    PICKER_SNAPSHOT_PREFIX,
    LocalPathError,
    PathCandidate,
    PickerChoice,
    ResolvedPath,
    perform_path_operation,
)


VALID_OPERATIONS = {"open", "reveal", "copy"}
MAX_SNAPSHOT_BYTES = 512 * 1024


def main() -> int:
    try:
        operation, choices = load_picker_snapshot()
        if not choices:
            raise LocalPathError("The path picker received no choices.")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise LocalPathError("The path picker needs an interactive terminal.")
        return run_picker(choices, operation)
    except LocalPathError as exc:
        print(f"local-path-actions: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive for popup logs.
        print(f"local-path-actions: unexpected picker error: {exc}", file=sys.stderr)
        return 1


def load_picker_snapshot() -> tuple[str, list[PickerChoice]]:
    snapshot_text = os.environ.get("LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT")
    state_dir_text = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not snapshot_text or not state_dir_text:
        raise LocalPathError("The picker snapshot environment is incomplete.")

    snapshot_path = Path(snapshot_text)
    state_dir = Path(state_dir_text)
    try:
        resolved_snapshot = snapshot_path.resolve(strict=True)
        resolved_state_dir = state_dir.resolve(strict=True)
    except OSError as exc:
        raise LocalPathError(f"Picker snapshot is unavailable: {exc}") from exc
    if resolved_snapshot.parent != resolved_state_dir or not resolved_snapshot.name.startswith(
        PICKER_SNAPSHOT_PREFIX
    ):
        raise LocalPathError("Picker snapshot path was rejected.")
    try:
        if resolved_snapshot.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise LocalPathError("Picker snapshot is too large.")
    except OSError as exc:
        raise LocalPathError(f"Picker snapshot is unavailable: {exc}") from exc

    try:
        data = json.loads(resolved_snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalPathError(f"Could not read picker snapshot: {exc}") from exc
    finally:
        resolved_snapshot.unlink(missing_ok=True)

    if data.get("schema_version") != 1:
        raise LocalPathError("Unsupported picker snapshot version.")
    operation = data.get("default_operation")
    if operation not in VALID_OPERATIONS:
        raise LocalPathError("Picker snapshot contains an invalid operation.")

    raw_choices = data.get("choices", [])
    if not isinstance(raw_choices, list) or len(raw_choices) > 30:
        raise LocalPathError("Picker snapshot contains an invalid number of choices.")
    choices: list[PickerChoice] = []
    try:
        for item in raw_choices:
            choices.append(
                PickerChoice(
                    candidate=PathCandidate(**item["candidate"]),
                    resolved=ResolvedPath(**item["resolved"]),
                )
            )
    except (KeyError, TypeError) as exc:
        raise LocalPathError(f"Picker snapshot contains invalid choices: {exc}") from exc
    return operation, choices


def run_picker(choices: list[PickerChoice], default_operation: str) -> int:
    selected = 0
    status = ""
    with terminal_input_mode():
        while True:
            render(choices, selected, default_operation, status)
            key = read_key()
            if key in {"escape", "q", "ctrl-c"}:
                return 0
            if key in {"up", "k"}:
                selected = (selected - 1) % len(choices)
                status = ""
                continue
            if key in {"down", "j"}:
                selected = (selected + 1) % len(choices)
                status = ""
                continue
            if key != "enter":
                continue
            operation = default_operation
            try:
                perform_path_operation(choices[selected].resolved, operation)
                if operation == "copy":
                    status = "Copied the selected path to the clipboard."
                    continue
                return 0
            except LocalPathError as exc:
                status = f"Could not complete the action: {exc}"


def render(
    choices: list[PickerChoice],
    selected: int,
    default_operation: str,
    status: str,
) -> None:
    size = shutil.get_terminal_size(fallback=(100, 28))
    width = max(20, size.columns)
    available_rows = max(1, size.lines - 7)
    start = max(0, min(selected - available_rows // 2, len(choices) - available_rows))
    end = min(len(choices), start + available_rows)

    lines = [
        "Local Path Picker",
        f"{len(choices)} files and folders found",
        f"Choose with ↑/↓, then press Enter to {operation_label(default_operation)} · Esc closes",
        "",
    ]
    rendered = [clip_line(line, width) for line in lines]
    for index in range(start, end):
        choice = choices[index]
        marker = "File" if choice.resolved.is_file else "Folder"
        pointer = ">" if index == selected else " "
        row = f"{pointer} [{marker}] {choice.resolved.local_path}"
        row = clip_line(row, width)
        if index == selected:
            row = f"\x1b[7m{row.ljust(width)}\x1b[0m"
        rendered.append(row)
    if status:
        rendered.extend(["", clip_line(status, width)])
    else:
        choice = choices[selected]
        rendered.extend(
            [
                "",
                clip_line(f"Selected path: {choice.resolved.local_path}", width),
                clip_line(f"Found in recent output: {choice.candidate.excerpt}", width),
            ]
        )

    sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(rendered[: size.lines]))
    sys.stdout.flush()


def operation_label(operation: str) -> str:
    return {
        "open": "open the selected item",
        "reveal": "show the selected item in its folder",
        "copy": "copy the selected path",
    }.get(operation, operation)


def clip_line(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def clip_middle(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    left = (width - 1) // 2
    right = width - left - 1
    return text[:left] + "…" + text[-right:]


class terminal_input_mode:
    def __enter__(self):
        self._windows = os.name == "nt"
        self._attrs = None
        if not self._windows:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self._windows and self._attrs is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._attrs)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        return False


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        char = msvcrt.getwch()
        if char in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "unknown")
        return decode_key(char)

    import select

    char = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    if char == "\x1b":
        sequence = ""
        while select.select([sys.stdin], [], [], 0.02)[0]:
            sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
        return decode_escape_sequence(sequence)
    return decode_key(char)


def decode_escape_sequence(sequence: str) -> str:
    legacy = {"[A": "up", "[B": "down"}.get(sequence)
    if legacy:
        return legacy
    kitty_key = re.fullmatch(r"\[(\d+)(?:;[0-9:]+)?u", sequence)
    if kitty_key:
        codepoint = int(kitty_key.group(1))
        try:
            return decode_key(chr(codepoint))
        except ValueError:
            return "unknown"
    return "escape"


def decode_key(char: str) -> str:
    if char in {"\r", "\n"}:
        return "enter"
    if char == "\x1b":
        return "escape"
    if char == "\x03":
        return "ctrl-c"
    if char in {"j", "k", "q"}:
        return char
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
