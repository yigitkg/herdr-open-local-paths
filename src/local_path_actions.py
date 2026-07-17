#!/usr/bin/env python3
"""Open and reveal local paths found in Herdr pane output."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse


HIGH_RISK_EXTENSIONS = {
    ".app",
    ".appimage",
    ".bat",
    ".cmd",
    ".com",
    ".command",
    ".cpl",
    ".desktop",
    ".exe",
    ".hta",
    ".jar",
    ".js",
    ".lnk",
    ".msi",
    ".pif",
    ".ps1",
    ".reg",
    ".run",
    ".scf",
    ".sh",
    ".scr",
    ".url",
    ".vbs",
    ".wsf",
    ".wsh",
}

WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/].+")
UNC_RE = re.compile(r"^\\\\[^\\/:*?\"<>|\r\n]+\\[^\\/:*?\"<>|\r\n]+")
WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/|$)")
LINE_SUFFIX_RE = re.compile(r"^(?P<path>.+?)(?::(?P<line>\d+)(?::(?P<column>\d+))?)$")
FILE_URL_CANDIDATE_RE = re.compile(r"file://[^\s\"'<>]+")
WINDOWS_CANDIDATE_RE = re.compile(r"(?<![\w/])(?:[a-zA-Z]:[\\/][^\s\"'<>|]+)")
UNC_CANDIDATE_RE = re.compile(r"\\\\[^\\/:*?\"<>|\s]+\\[^\\/:*?\"<>|\s]+(?:\\[^\\/:*?\"<>|\s]+)*")
POSIX_CANDIDATE_RE = re.compile(r"(?<![\w])(?:~|/)[^\s\"'<>]+")
RELATIVE_CANDIDATE_RE = re.compile(
    r"(?<![\w./\\-])(?:\.{1,2}[\\/][^\s\"'<>]+|[A-Za-z0-9_.@-]+(?:[\\/][A-Za-z0-9_.@ -]+)+\.[A-Za-z0-9]{1,12}(?::\d+(?::\d+)?)?)"
)
DELIMITED_CANDIDATE_RE = re.compile(r"(?P<quote>['\"`])(?P<body>[^\r\n]+?)(?P=quote)")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]\r\n]*\]\((?P<body>[^\r\n)]+)\)")
MAX_SCAN_CANDIDATES = 80
MAX_PICKER_CHOICES = 30
DEFAULT_SCAN_LINES = 120
MIN_SCAN_LINES = 20
MAX_SCAN_LINES = 500
PICKER_SNAPSHOT_TTL_SECONDS = 60 * 60
PICKER_SNAPSHOT_PREFIX = "path-picker-"
WRAPPER_PAIRS = {
    "'": "'",
    '"': '"',
    "`": "`",
    "(": ")",
    "[": "]",
    "{": "}",
    "<": ">",
}


class LocalPathError(Exception):
    """Expected plugin error."""


@dataclass
class PluginContext:
    workspace_cwd: str | None = None
    focused_pane_cwd: str | None = None
    selected_text: str | None = None
    clicked_url: str | None = None
    link_handler_id: str | None = None
    invocation_source: str | None = None
    focused_pane_id: str | None = None
    workspace_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ResolvedPath:
    source: str
    original_text: str
    stripped_text: str
    path_text: str
    local_path: str
    path_kind: str
    host_platform: str
    line: int | None = None
    column: int | None = None
    exists: bool = False
    is_file: bool = False
    is_dir: bool = False
    parent_exists: bool = False
    warning: str | None = None


@dataclass(frozen=True)
class PathCandidate:
    """A path-like occurrence in pane output, with display metadata."""

    text: str
    offset: int
    line: int
    column: int
    excerpt: str


@dataclass
class PickerChoice:
    """A resolved existing path displayed by the interactive picker."""

    candidate: PathCandidate
    resolved: ResolvedPath


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "diagnose"
    try:
        context = load_context()
        if action == "diagnose":
            try:
                resolved = resolve_from_context(context)
            except LocalPathError as exc:
                print_diagnose(context, None, str(exc))
                return 0
            print_diagnose(context, resolved, None)
            return 0
        if action == "diagnose-latest":
            print_latest_diagnose(context)
            return 0
        if action == "open-latest":
            run_adaptive_path_action(context, "open")
            return 0
        if action == "reveal-latest":
            run_adaptive_path_action(context, "reveal")
            return 0
        if action == "copy-latest":
            run_adaptive_path_action(context, "copy")
            return 0
        resolved = resolve_from_context(context)
        if action == "open":
            ensure_context_action_allowed(context, resolved, "open")
            open_path(resolved)
            return 0
        if action == "reveal":
            ensure_context_action_allowed(context, resolved, "reveal")
            reveal_path(resolved)
            return 0
        if action == "copy":
            copy_text(resolved.local_path)
            print(resolved.local_path)
            return 0
        raise LocalPathError(f"Unknown action: {action}")
    except LocalPathError as exc:
        print(f"local-path-actions: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive for plugin logs.
        print(f"local-path-actions: unexpected error: {exc}", file=sys.stderr)
        return 1


def load_context() -> PluginContext:
    raw_context = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}"
    try:
        data = json.loads(raw_context)
    except json.JSONDecodeError:
        data = {}

    clicked_url = os.environ.get("HERDR_PLUGIN_CLICKED_URL") or data.get("clicked_url")
    link_handler_id = os.environ.get("HERDR_PLUGIN_LINK_HANDLER_ID") or data.get("link_handler_id")

    return PluginContext(
        workspace_cwd=as_optional_str(data.get("workspace_cwd")),
        focused_pane_cwd=as_optional_str(data.get("focused_pane_cwd")),
        selected_text=as_optional_str(data.get("selected_text")),
        clicked_url=as_optional_str(clicked_url),
        link_handler_id=as_optional_str(link_handler_id),
        invocation_source=as_optional_str(data.get("invocation_source")),
        focused_pane_id=as_optional_str(data.get("focused_pane_id")),
        workspace_id=as_optional_str(data.get("workspace_id")),
        raw=data,
    )


def as_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def resolve_from_context(context: PluginContext) -> ResolvedPath:
    if context.clicked_url:
        return resolve_candidate(context.clicked_url, "clicked_url", context)
    if context.selected_text:
        return resolve_candidate(context.selected_text, "selected_text", context)
    raise LocalPathError("No selected text or clicked file URL was provided by Herdr.")


def resolve_latest_path_from_pane(context: PluginContext) -> ResolvedPath:
    choices = collect_picker_choices(context)
    if not choices:
        raise LocalPathError("No existing local path found in recent pane output.")
    newest_file = next((choice.resolved for choice in choices if choice.resolved.is_file), None)
    return newest_file or choices[0].resolved


def collect_picker_choices(context: PluginContext, output: str | None = None) -> list[PickerChoice]:
    """Resolve pane paths once and return unique existing choices newest-first."""
    if output is None:
        output = read_recent_pane_output(context)
    records = extract_path_candidate_records(output)
    newest_by_local_path: dict[str, PickerChoice] = {}
    for candidate in records[-MAX_SCAN_CANDIDATES:]:
        try:
            resolved = resolve_candidate(candidate.text, "recent_pane_output", context)
        except LocalPathError:
            continue
        if not resolved.exists or not (resolved.is_file or resolved.is_dir):
            continue
        choice = PickerChoice(candidate=candidate, resolved=resolved)
        newest_by_local_path[resolved_path_identity(resolved)] = choice
    return sorted(
        newest_by_local_path.values(),
        key=lambda choice: (not choice.resolved.is_file, -choice.candidate.offset),
    )[:MAX_PICKER_CHOICES]


def resolved_path_identity(resolved: ResolvedPath) -> str:
    if is_windows_path(resolved.local_path):
        return "windows:" + str(PureWindowsPath(resolved.local_path)).casefold()
    return "local:" + os.path.normcase(os.path.normpath(resolved.local_path))


def run_adaptive_path_action(context: PluginContext, operation: str) -> None:
    choices = collect_picker_choices(context)
    if not choices:
        raise LocalPathError("No existing local path found in recent pane output.")
    if operation in {"open", "reveal"}:
        locality = pane_locality(context)
        if locality == "remote" and os.environ.get("LOCAL_PATH_ACTIONS_ALLOW_REMOTE") != "1":
            raise LocalPathError(
                "The focused pane appears to be remote (SSH or a container). "
                "Open/reveal was refused to prevent opening a same-named local file."
            )
        if locality == "unknown" and os.environ.get("LOCAL_PATH_ACTIONS_ALLOW_REMOTE") != "1":
            choices = [choice for choice in choices if choice.resolved.path_kind != "relative"]
            if not choices:
                raise LocalPathError(
                    "Could not verify whether the focused pane is local; relative paths were refused."
                )
    if len(choices) == 1:
        perform_path_operation(choices[0].resolved, operation)
        return
    launch_path_picker(choices, operation)


def ensure_context_action_allowed(
    context: PluginContext, resolved: ResolvedPath, operation: str
) -> None:
    """Refuse ambiguous host actions when the pane appears remote."""
    if operation not in {"open", "reveal"} or os.environ.get("LOCAL_PATH_ACTIONS_ALLOW_REMOTE") == "1":
        return
    locality = pane_locality(context)
    if locality == "remote":
        raise LocalPathError(
            "The focused pane appears to be remote (SSH or a container). "
            "Open/reveal was refused to prevent opening a same-named local file."
        )
    if locality == "unknown" and resolved.path_kind == "relative":
        raise LocalPathError(
            "Could not verify whether the focused pane is local; the relative path was refused."
        )


def pane_locality(context: PluginContext) -> str:
    """Return local, remote, or unknown from Herdr's foreground process data."""
    pane_id = context.focused_pane_id or os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        return "unknown"
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        result = subprocess.run(
            [herdr, "pane", "process-info", "--pane", pane_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        if result.returncode != 0:
            return "unknown"
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return "unknown"

    process_info = data.get("result", {}).get("process_info", {})
    processes = process_info.get("foreground_processes", [])
    if not isinstance(processes, list):
        return "unknown"
    for process in processes:
        if not isinstance(process, dict):
            continue
        argv = process.get("argv")
        if not isinstance(argv, list):
            argv = []
        words = [str(word).casefold() for word in argv]
        name = str(process.get("name") or "").casefold()
        executable = Path(words[0]).name if words else name
        if executable in {"ssh", "mosh", "mosh-client"}:
            return "remote"
        if executable in {"docker", "podman", "kubectl"} and "exec" in words[1:]:
            return "remote"
    return "local"


def perform_path_operation(resolved: ResolvedPath, operation: str) -> None:
    # Re-check at activation time because the file may have changed since scan.
    exists, is_file, is_dir, parent_exists = inspect_path_for_resolved(resolved)
    resolved.exists = exists
    resolved.is_file = is_file
    resolved.is_dir = is_dir
    resolved.parent_exists = parent_exists
    if operation == "open":
        open_path(resolved)
    elif operation == "reveal":
        reveal_path(resolved)
    elif operation == "copy":
        copy_text(resolved.local_path)
        print(resolved.local_path)
    else:
        raise LocalPathError(f"Unsupported picker operation: {operation}")


def launch_path_picker(choices: list[PickerChoice], operation: str) -> None:
    snapshot_path = write_picker_snapshot(choices, operation)
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    plugin_id = os.environ.get("HERDR_PLUGIN_ID") or "yigitkg.local-path-actions"
    command = [
        herdr,
        "plugin",
        "pane",
        "open",
        "--plugin",
        plugin_id,
        "--entrypoint",
        "path-picker",
        "--env",
        f"LOCAL_PATH_ACTIONS_PICKER_SNAPSHOT={snapshot_path}",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        snapshot_path.unlink(missing_ok=True)
        raise LocalPathError(f"Could not open path picker: {exc}") from exc
    if result.returncode != 0:
        snapshot_path.unlink(missing_ok=True)
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Herdr error"
        raise LocalPathError(f"Could not open path picker: {detail}")


def write_picker_snapshot(choices: list[PickerChoice], operation: str) -> Path:
    state_dir_text = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir_text:
        raise LocalPathError("Herdr did not provide HERDR_PLUGIN_STATE_DIR.")
    state_dir = Path(state_dir_text)
    state_dir.mkdir(parents=True, exist_ok=True)
    prune_stale_picker_snapshots(state_dir)
    snapshot_path = state_dir / f"{PICKER_SNAPSHOT_PREFIX}{uuid.uuid4().hex}.json"
    payload = {
        "schema_version": 1,
        "default_operation": operation,
        "choices": [
            {
                "candidate": asdict(choice.candidate),
                "resolved": asdict(choice.resolved),
            }
            for choice in choices
        ],
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(snapshot_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except Exception:
        snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path


def prune_stale_picker_snapshots(state_dir: Path, now: float | None = None) -> None:
    """Remove only this plugin's abandoned picker payloads after a safe TTL."""
    cutoff = (time.time() if now is None else now) - PICKER_SNAPSHOT_TTL_SECONDS
    try:
        candidates = state_dir.glob(f"{PICKER_SNAPSHOT_PREFIX}*.json")
        for candidate in candidates:
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return


def diagnose_latest_path_from_pane(context: PluginContext) -> dict[str, Any]:
    output = read_recent_pane_output(context)
    candidates = extract_path_candidates(output)
    checked: list[dict[str, Any]] = []
    selected_file: ResolvedPath | None = None
    selected_dir: ResolvedPath | None = None
    for candidate in reversed(candidates[-MAX_SCAN_CANDIDATES:]):
        try:
            resolved = resolve_candidate(candidate, "recent_pane_output", context)
            checked.append({"candidate": candidate, "resolved": asdict(resolved), "error": None})
            if selected_file is None and resolved.exists and resolved.is_file:
                selected_file = resolved
            if selected_dir is None and resolved.exists and resolved.is_dir:
                selected_dir = resolved
        except LocalPathError as exc:
            checked.append({"candidate": candidate, "resolved": None, "error": str(exc)})
    selected = selected_file or selected_dir
    return {
        "candidate_count": len(candidates),
        "candidates": candidates[-20:],
        "selected": asdict(selected) if selected else None,
        "checked_newest_first": checked[:20],
    }


def read_recent_pane_output(context: PluginContext) -> str:
    pane_id = context.focused_pane_id or os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        raise LocalPathError("Herdr did not provide a focused pane id.")
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    lines = scan_line_limit(os.environ.get("LOCAL_PATH_ACTIONS_SCAN_LINES"))
    command = [
        herdr,
        "pane",
        "read",
        pane_id,
        "--source",
        "recent-unwrapped",
        "--lines",
        str(lines),
    ]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown error"
        raise LocalPathError(f"Could not read recent pane output: {stderr}")
    return result.stdout


def scan_line_limit(raw_value: str | None) -> int:
    if raw_value is None:
        return DEFAULT_SCAN_LINES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_SCAN_LINES
    return max(MIN_SCAN_LINES, min(value, MAX_SCAN_LINES))


def extract_path_candidates(text: str) -> list[str]:
    """Return candidate text in occurrence order, deduplicated newest-first.

    The last occurrence of a normalized path wins. This keeps the result useful
    for the legacy "latest path" actions while the richer records below can be
    displayed by an interactive picker.
    """
    return [candidate.text for candidate in extract_path_candidate_records(text)]


def extract_path_candidate_records(text: str) -> list[PathCandidate]:
    stripped = strip_ansi(text)
    candidates: list[tuple[int, int, str]] = []
    delimited_spans: list[tuple[int, int]] = []

    # Markdown destinations, quoting, and code spans give us unambiguous
    # boundaries, so these candidates may safely contain spaces.
    for match in MARKDOWN_LINK_RE.finditer(stripped):
        candidate = clean_candidate_from_scan(match.group("body").strip("<>"))
        if candidate and looks_like_path(candidate):
            start, end = match.span("body")
            candidates.append((start, end, candidate))
            delimited_spans.append(match.span())

    # Quoting or Markdown code spans give us an unambiguous boundary, so these
    # candidates may safely contain spaces.
    for match in DELIMITED_CANDIDATE_RE.finditer(stripped):
        candidate = clean_candidate_from_scan(match.group("body"))
        if candidate and looks_like_path(candidate):
            start, end = match.span("body")
            candidates.append((start, end, candidate))
            delimited_spans.append(match.span())

    for pattern in (
        FILE_URL_CANDIDATE_RE,
        WINDOWS_CANDIDATE_RE,
        UNC_CANDIDATE_RE,
        POSIX_CANDIDATE_RE,
        RELATIVE_CANDIDATE_RE,
    ):
        for match in pattern.finditer(stripped):
            if any(start <= match.start() and match.end() <= end for start, end in delimited_spans):
                continue
            candidate = clean_candidate_from_scan(match.group(0))
            if candidate and looks_like_path(candidate):
                candidates.append((match.start(), match.end(), candidate))
    candidates.sort(key=lambda item: item[0])

    # Regexes can overlap (for example a POSIX match inside a file URL). Keep
    # the widest candidate at a given occurrence before global deduplication.
    non_overlapping: list[tuple[int, int, str]] = []
    for start, end, candidate in candidates:
        if non_overlapping and start >= non_overlapping[-1][0] and end <= non_overlapping[-1][1]:
            continue
        non_overlapping.append((start, end, candidate))

    # Assigning by key replaces older occurrences; sorting afterwards restores
    # pane order while ensuring the latest spelling/line metadata is retained.
    newest_by_path: dict[str, tuple[int, int, str]] = {}
    for item in non_overlapping:
        newest_by_path[normalized_candidate_key(item[2])] = item

    records: list[PathCandidate] = []
    for start, end, candidate in sorted(newest_by_path.values(), key=lambda item: item[0]):
        line_start = stripped.rfind("\n", 0, start) + 1
        line_end = stripped.find("\n", end)
        if line_end == -1:
            line_end = len(stripped)
        records.append(
            PathCandidate(
                text=candidate,
                offset=start,
                line=stripped.count("\n", 0, start) + 1,
                column=start - line_start + 1,
                excerpt=stripped[line_start:line_end].strip(),
            )
        )
    return records


def looks_like_path(candidate: str) -> bool:
    # Bare slash tokens are common in command/help output and only add a noisy
    # filesystem-root choice to the picker.
    if candidate and not candidate.strip("/"):
        return False
    try:
        path_from_text(candidate)
        return True
    except LocalPathError:
        return False


def normalized_candidate_key(candidate: str) -> str:
    """Create a stable identity key without needing filesystem access."""
    cleaned = clean_candidate_from_scan(candidate)
    try:
        path_text, path_kind = path_from_text(cleaned)
        path_text, _, _ = split_line_suffix(path_text, path_kind)
        if path_kind == "file-url":
            path_text = file_url_to_path(cleaned)
            if not is_windows_path(path_text):
                return "posix:" + os.path.normpath(path_text)
        if path_kind in {"windows-drive", "unc", "file-url"} and is_windows_path(path_text):
            return "windows:" + str(PureWindowsPath(path_text.replace("/", "\\"))).casefold()
        return path_kind + ":" + os.path.normpath(path_text)
    except (LocalPathError, OSError, ValueError):
        return "raw:" + cleaned


def clean_candidate_from_scan(candidate: str) -> str:
    candidate = candidate.strip()
    candidate = unwrap_candidate(candidate)
    candidate = trim_trailing_punctuation(candidate)
    while candidate and candidate[-1] in ")]}":
        opens = {"(": ")", "[": "]", "{": "}"}
        matching_open = next((op for op, close in opens.items() if close == candidate[-1]), None)
        if matching_open and candidate.count(matching_open) >= candidate.count(candidate[-1]):
            break
        candidate = candidate[:-1].rstrip()
    return candidate


def resolve_candidate(text: str, source: str, context: PluginContext) -> ResolvedPath:
    if "\x00" in text:
        raise LocalPathError("Path contains a NUL byte and was rejected.")
    cleaned = strip_ansi(text).strip()
    cleaned = unwrap_candidate(cleaned)
    if is_windows_path(cleaned):
        ensure_windows_path_is_canonical(cleaned)
    cleaned = trim_trailing_punctuation(cleaned)
    path_text, path_kind = path_from_text(cleaned)
    path_without_line, line, column = split_line_suffix(path_text, path_kind)
    host = host_platform()
    local_path, normalized_kind, warning = normalize_path(path_without_line, path_kind, context, host)
    resolved = ResolvedPath(
        source=source,
        original_text=text,
        stripped_text=cleaned,
        path_text=path_without_line,
        local_path=local_path,
        path_kind=normalized_kind,
        host_platform=host,
        line=line,
        column=column,
        warning=warning,
    )
    if is_network_path(resolved):
        resolved.warning = "Network/UNC path was skipped to avoid a blocking filesystem check."
        return resolved
    exists, is_file, is_dir, parent_exists = inspect_path_for_resolved(resolved)
    resolved.exists = exists
    resolved.is_file = is_file
    resolved.is_dir = is_dir
    resolved.parent_exists = parent_exists
    return resolved


def is_network_path(resolved: ResolvedPath) -> bool:
    if resolved.path_kind == "wsl-unc":
        return False
    return resolved.path_kind == "unc" or (
        resolved.path_kind == "file-url" and bool(UNC_RE.match(resolved.local_path))
    )


def strip_ansi(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1bP.*?\x1b\\", "", text, flags=re.DOTALL)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def unwrap_candidate(text: str) -> str:
    previous = None
    while previous != text and len(text) >= 2:
        previous = text
        start, end = text[0], text[-1]
        if WRAPPER_PAIRS.get(start) == end:
            text = text[1:-1].strip()
    return text


def trim_trailing_punctuation(text: str) -> str:
    while text and text[-1] in ",;":
        text = text[:-1].rstrip()
    if text.endswith(".") and not re.search(r"\.\w{1,12}$", text):
        text = text[:-1].rstrip()
    return text


def path_from_text(text: str) -> tuple[str, str]:
    if WINDOWS_DRIVE_RE.match(text):
        return text, "windows-drive"
    if UNC_RE.match(text):
        return text, "unc"
    parsed = urlparse(text)
    if parsed.scheme == "file":
        return file_url_to_path(text), "file-url"
    if parsed.scheme and parsed.scheme != "file":
        raise LocalPathError(f"Unsupported URL scheme: {parsed.scheme}")
    if text.startswith("~/") or text == "~":
        return text, "home"
    if text.startswith("/"):
        if WSL_MOUNT_RE.match(text):
            return text, "wsl-mount"
        return text, "posix"
    if (
        text.startswith("./")
        or text.startswith("../")
        or re.match(r"^[^\\/]+[\\/].+", text)
        or re.match(r"^[A-Za-z0-9_.@ -]+\.[A-Za-z0-9]{1,12}$", text)
    ):
        return text, "relative"
    raise LocalPathError("Selected text does not look like a supported local path.")


def file_url_to_path(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise LocalPathError(f"Unsupported URL scheme: {parsed.scheme}")
    if not parsed.netloc and not parsed.path:
        raise LocalPathError("File URL does not contain a path.")
    path = unquote(parsed.path)
    if any(ord(char) < 32 for char in path):
        raise LocalPathError("Decoded file URL contains control characters and was rejected.")
    if parsed.netloc.casefold() == "localhost":
        parsed = parsed._replace(netloc="")
    if parsed.netloc and re.match(r"^[a-zA-Z]:$", parsed.netloc):
        return f"{parsed.netloc}{path}"
    if parsed.netloc:
        return "\\\\" + parsed.netloc + path.replace("/", "\\")
    if re.match(r"^/[a-zA-Z]:/", path):
        return path[1:].replace("/", "\\")
    return path


def split_line_suffix(path_text: str, path_kind: str) -> tuple[str, int | None, int | None]:
    if path_kind in {"windows-drive", "unc"}:
        # Keep C: intact, but allow C:\x\file.py:12.
        match = LINE_SUFFIX_RE.match(path_text)
        if match and not re.match(r"^[a-zA-Z]:$", match.group("path")):
            return suffix_match(match)
        return path_text, None, None
    match = LINE_SUFFIX_RE.match(path_text)
    if not match:
        return path_text, None, None
    candidate_path = match.group("path")
    if "/" not in candidate_path and "\\" not in candidate_path and not Path(candidate_path).suffix:
        return path_text, None, None
    return suffix_match(match)


def suffix_match(match: re.Match[str]) -> tuple[str, int | None, int | None]:
    line = int(match.group("line")) if match.group("line") else None
    column = int(match.group("column")) if match.group("column") else None
    return match.group("path"), line, column


def normalize_path(
    path_text: str,
    path_kind: str,
    context: PluginContext,
    host: str,
) -> tuple[str, str, str | None]:
    if path_kind == "home":
        return str(Path(path_text).expanduser()), "home", None
    if path_kind == "relative":
        base = context.focused_pane_cwd or context.workspace_cwd
        if not base:
            raise LocalPathError("Relative path needs focused_pane_cwd or workspace_cwd from Herdr.")
        resolved = str(Path(base, path_text).resolve(strict=False))
        if is_wsl_environment() and WSL_MOUNT_RE.match(resolved):
            win_path = wsl_mount_to_windows(resolved)
            if win_path:
                return win_path, "wsl-mount-windows", None
        return resolved, "relative", None
    if path_kind == "wsl-mount" and is_wsl_environment():
        win_path = wsl_mount_to_windows(path_text)
        if win_path:
            return win_path, "wsl-mount-windows", None
    if path_kind == "posix" and is_wsl_environment() and os.environ.get("WSL_DISTRO_NAME"):
        distro = os.environ["WSL_DISTRO_NAME"]
        return wsl_unc_path(path_text, distro), "wsl-unc", None
    if path_kind in {"windows-drive", "unc"}:
        return normalize_windows_text(path_text), path_kind, None
    return str(Path(path_text).expanduser()), path_kind, None


def normalize_windows_text(path_text: str) -> str:
    if UNC_RE.match(path_text):
        return str(PureWindowsPath(path_text))
    return str(PureWindowsPath(path_text.replace("/", "\\")))


def is_wsl_environment() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def wsl_mount_to_windows(path_text: str) -> str | None:
    match = WSL_MOUNT_RE.match(path_text)
    if not match:
        return None
    drive = match.group(1).upper()
    rest = path_text[len(match.group(0)) :].replace("/", "\\")
    return f"{drive}:\\" + rest


def wsl_unc_path(path_text: str, distro: str) -> str:
    return "\\\\wsl$" + f"\\{distro}" + path_text.replace("/", "\\")


def windows_drive_to_wsl_mount(path_text: str) -> str | None:
    if not WINDOWS_DRIVE_RE.match(path_text):
        return None
    windows_path = PureWindowsPath(path_text)
    drive = windows_path.drive.rstrip(":").lower()
    if len(drive) != 1 or not drive.isalpha():
        return None
    relative_parts = windows_path.parts[1:]
    return str(Path("/mnt", drive, *relative_parts))


def inspect_path_for_resolved(resolved: ResolvedPath) -> tuple[bool, bool, bool, bool]:
    checks = [resolved.local_path]
    if is_wsl_environment():
        wsl_mount = windows_drive_to_wsl_mount(resolved.local_path)
        if wsl_mount:
            checks.append(wsl_mount)
        if resolved.path_kind in {"wsl-mount-windows", "wsl-unc"}:
            checks.append(resolved.path_text)
    for candidate in dict.fromkeys(checks):
        inspected = inspect_path(candidate)
        if inspected[0]:
            return inspected
    return inspect_path(checks[-1])


def inspect_path(local_path: str) -> tuple[bool, bool, bool, bool]:
    try:
        path = Path(local_path)
        exists = path.exists()
        is_file = path.is_file()
        is_dir = path.is_dir()
        parent_exists = path.parent.exists()
        return exists, is_file, is_dir, parent_exists
    except (OSError, ValueError):
        return False, False, False, False


def open_path(resolved: ResolvedPath) -> None:
    ensure_open_allowed(resolved)
    command = open_command(resolved.local_path, resolved.host_platform)
    run_platform_command(command)


def reveal_path(resolved: ResolvedPath) -> None:
    ensure_path_exists_for_action(resolved)
    command = reveal_command(resolved.local_path, resolved.host_platform, resolved.is_dir)
    run_platform_command(command)


def ensure_open_allowed(resolved: ResolvedPath) -> None:
    ensure_windows_path_is_canonical(resolved.local_path)
    ensure_path_exists_for_action(resolved)
    suffix = suffix_for_path(resolved.local_path)
    if suffix in HIGH_RISK_EXTENSIONS and os.environ.get("LOCAL_PATH_ACTIONS_ALLOW_RISKY") != "1":
        raise LocalPathError(
            f"{suffix} files can run code. Reveal it instead, or set LOCAL_PATH_ACTIONS_ALLOW_RISKY=1."
        )
    if is_posix_executable(resolved) and os.environ.get("LOCAL_PATH_ACTIONS_ALLOW_RISKY") != "1":
        raise LocalPathError(
            "Executable files can run code. Reveal it instead, or set "
            "LOCAL_PATH_ACTIONS_ALLOW_RISKY=1."
        )


def ensure_windows_path_is_canonical(path_text: str) -> None:
    """Reject Win32 aliases that can bypass extension-based safety checks."""
    if not is_windows_path(path_text):
        return
    path = PureWindowsPath(path_text)
    for part in path.parts[1:]:
        if part.rstrip(" .") != part:
            raise LocalPathError("Windows paths with trailing dots or spaces are refused.")
        if ":" in part:
            raise LocalPathError("Windows alternate data stream paths are refused.")


def is_posix_executable(resolved: ResolvedPath) -> bool:
    if resolved.host_platform == "windows" or resolved.path_kind not in {
        "posix",
        "home",
        "relative",
        "wsl-unc",
    }:
        return False
    check_path = resolved.path_text if resolved.path_kind == "wsl-unc" else resolved.local_path
    try:
        path = Path(check_path)
        return path.is_file() and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def ensure_path_exists_for_action(resolved: ResolvedPath) -> None:
    if resolved.exists:
        return
    if resolved.parent_exists:
        raise LocalPathError(f"Path does not exist, but parent exists: {resolved.local_path}")
    raise LocalPathError(f"Path does not exist: {resolved.local_path}")


def suffix_for_path(path_text: str) -> str:
    if WINDOWS_DRIVE_RE.match(path_text) or UNC_RE.match(path_text):
        return PureWindowsPath(path_text).suffix.lower()
    return Path(path_text).suffix.lower()


def host_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def open_command(path_text: str, host: str) -> tuple[str, list[str]]:
    if is_windows_path(path_text) and (host == "windows" or is_wsl_environment()):
        if host == "windows":
            return "os.startfile", [path_text]
        return "explorer.exe", [path_text]
    if host == "macos":
        return "open", [path_text]
    if host == "windows":
        return "os.startfile", [path_text]
    return "xdg-open", [path_text]


def reveal_command(path_text: str, host: str, is_dir: bool) -> tuple[str, list[str]]:
    if is_windows_path(path_text) and (host == "windows" or is_wsl_environment()):
        if is_dir:
            return "explorer.exe", [path_text]
        return "explorer.exe", [f"/select,{path_text}"]
    if host == "macos":
        if is_dir:
            return "open", [path_text]
        return "open", ["-R", path_text]
    if host == "windows":
        if is_dir:
            return "explorer.exe", [path_text]
        return "explorer.exe", [f"/select,{path_text}"]
    parent = str(Path(path_text).parent) if not is_dir else path_text
    return "xdg-open", [parent]


def is_windows_path(path_text: str) -> bool:
    return bool(WINDOWS_DRIVE_RE.match(path_text) or UNC_RE.match(path_text))


def run_platform_command(command: tuple[str, list[str]]) -> None:
    exe, args = command
    if exe == "os.startfile":
        run_windows_startfile(args[0])
        return
    run_command(exe, args)


def run_windows_startfile(path_text: str) -> None:
    if os.environ.get("LOCAL_PATH_ACTIONS_DRY_RUN") == "1":
        print(json.dumps({"command": ["os.startfile", path_text]}, indent=2))
        return
    if not hasattr(os, "startfile"):
        raise LocalPathError("os.startfile is unavailable on this Python runtime.")
    os.startfile(path_text)  # type: ignore[attr-defined]


def run_command(exe: str, args: list[str]) -> None:
    if os.environ.get("LOCAL_PATH_ACTIONS_DRY_RUN") == "1":
        print(json.dumps({"command": [exe, *args]}, indent=2))
        return
    if shutil.which(exe) is None:
        raise LocalPathError(f"Required command was not found on PATH: {exe}")
    if exe.lower() == "explorer.exe":
        try:
            result = subprocess.run(
                [exe, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalPathError("Windows File Explorer did not respond in time.") from exc
        except OSError as exc:
            raise LocalPathError(f"Could not start Windows File Explorer: {exc}") from exc
        # Windows Explorer commonly returns 1 after a successful ShellExecute
        # handoff when launched through WSL interop.
        if result.returncode not in {0, 1}:
            raise LocalPathError(
                f"Windows File Explorer rejected the request with exit code {result.returncode}."
            )
        return
    subprocess.Popen([exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_text(text: str) -> None:
    commands: list[tuple[str, list[str]]] = []
    if is_wsl_environment() or host_platform() == "windows":
        commands.extend([("clip.exe", []), ("powershell.exe", ["-NoProfile", "-Command", "Set-Clipboard"])])
    elif host_platform() == "macos":
        commands.append(("pbcopy", []))
    else:
        commands.extend([("wl-copy", []), ("xclip", ["-selection", "clipboard"]), ("xsel", ["--clipboard", "--input"])])

    for exe, args in commands:
        if shutil.which(exe):
            try:
                proc = subprocess.run(
                    [exe, *args],
                    input=text,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0:
                return
    print(text)
    raise LocalPathError("No clipboard command succeeded. The resolved path was printed to stdout.")


def print_diagnose(context: PluginContext, resolved: ResolvedPath | None, resolution_error: str | None = None) -> None:
    output = {
        "context": {
            "workspace_id": context.workspace_id,
            "workspace_cwd": context.workspace_cwd,
            "focused_pane_id": context.focused_pane_id,
            "focused_pane_cwd": context.focused_pane_cwd,
            "selected_text": context.selected_text,
            "clicked_url": context.clicked_url,
            "link_handler_id": context.link_handler_id,
            "invocation_source": context.invocation_source,
        },
        "resolved": asdict(resolved) if resolved else None,
        "resolution_error": resolution_error,
        "environment": {
            "platform": host_platform(),
            "is_wsl": is_wsl_environment(),
            "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        },
    }
    print(json.dumps(output, indent=2))


def print_latest_diagnose(context: PluginContext) -> None:
    output = {
        "context": {
            "workspace_id": context.workspace_id,
            "workspace_cwd": context.workspace_cwd,
            "focused_pane_id": context.focused_pane_id,
            "focused_pane_cwd": context.focused_pane_cwd,
            "invocation_source": context.invocation_source,
        },
        "latest_scan": diagnose_latest_path_from_pane(context),
        "environment": {
            "platform": host_platform(),
            "is_wsl": is_wsl_environment(),
            "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        },
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
