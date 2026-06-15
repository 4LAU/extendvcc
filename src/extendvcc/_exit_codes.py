"""Stable process exit codes for the extendvcc CLI.

These integers are part of the CLI's contract: scripts and CI pipelines can
branch on them. Map exceptions to codes in ``cli.main`` — do not invent new
codes ad hoc.

| Code | Name               | Meaning                                              |
|------|--------------------|------------------------------------------------------|
| 0    | EXIT_OK            | Success.                                             |
| 1    | EXIT_ERROR         | Generic failure (library error, aborted confirm).   |
| 2    | EXIT_USAGE         | CLI input/usage error (bad flags, validation).      |
| 3    | EXIT_AUTH_REQUIRED | Authentication needed or failed (login/OTP/session).|
| 4    | EXIT_DISABLED      | Kill switch / account-risk: automation disabled.    |
| 5    | EXIT_API_ERROR     | Extend API returned an error response.              |
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 3
EXIT_DISABLED = 4
EXIT_API_ERROR = 5
