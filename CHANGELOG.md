# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- File rows in the picker now show the complete path, matching folder rows.

## [0.3.0] - 2026-07-18

### Added

- Adaptive picker for multiple existing files and folders.
- Quoted, backticked, Markdown-linked, Windows, WSL, POSIX, and relative path detection.
- File-first ordering, deduplication, source excerpts, and plain-language item labels.
- Remote-pane checks for SSH, Mosh, and common container exec commands.
- Automated tests and release CI.

### Changed

- The launch shortcut now determines the picker's Enter action.
- Initial supported platform claim is limited to Linux and WSL.
- Popup size is reduced to 75% width and 55% height.

### Security

- Risky extensions and extensionless POSIX executables are refused by open.
- Win32 trailing-dot, trailing-space, and alternate-data-stream aliases are refused.
- Network path checks, subprocess calls, clipboard calls, and picker snapshots are bounded.
- Decoded file URLs and terminal control sequences are validated before use.

[Unreleased]: https://github.com/yigitkg/herdr-open-local-paths/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/yigitkg/herdr-open-local-paths/releases/tag/v0.3.0
