# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not open a public issue for a vulnerability. If private reporting is unavailable, contact the repository owner through the email address published on the owner's GitHub profile.

Include the affected version, operating environment, reproduction steps, and expected impact. You should receive an initial response within seven days.

## Security model

Terminal output is untrusted input. The plugin parses paths from recent output, checks that candidates exist locally, and invokes platform tools with argument arrays rather than shell interpolation.

The open action refuses high-risk executable types and executable POSIX files. UNC/network paths are not probed. Remote panes are detected from foreground process information; open and reveal are refused for recognized SSH, Mosh, and container exec sessions. Because process detection cannot prove locality in every nested setup, users must not act on paths they do not trust.

Picker snapshots are stored in Herdr's plugin state directory with user-only permissions, random names, bounded size/count, single-use deletion, and stale-file cleanup. The plugin is not a sandbox and inherits the user's filesystem permissions.
