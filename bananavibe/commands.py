"""Issue commands. Only a comment that is entirely a command is executable;
command-like text inside ordinary discussion is never acted on."""

from dataclasses import dataclass
import re

NAMES = ("help", "start", "status", "stop", "resume", "restart", "models", "model", "answer", "fail")
NEEDS_ARGUMENT = {"model", "answer", "fail"}
ALIASES = {"revise": "answer", "cancel": "stop", "continue": "resume"}
PATTERN = re.compile(r"\s*/banana(?:vibe)?(?:[ \t]+([a-z]+))?(?:(?:[ \t]*\r?\n|[ \t]+)([\s\S]*))?", re.IGNORECASE)

HELP = """**BananaVibe** prepares checked drafts for maintainer review. Commands must be the whole comment.

| Command | Effect |
| --- | --- |
| `/banana start` | Begin work on this issue. |
| `/banana status` | Show the task state, branch, checkpoint and pull request. |
| `/banana stop` | Stop at the next safe point, keeping the saved branch. |
| `/banana resume` | Continue from the saved branch, or approve an edited issue. |
| `/banana answer TEXT` | Give guidance and continue. On a finished task, revise its open pull request. |
| `/banana restart` | Start over from the base branch on a new branch (the old one is kept). |
| `/banana models` | List the configured model aliases. |
| `/banana model ALIAS` | Switch model; a running task switches at its next safe point. |
| `/banana fail REASON` | Mark the task failed and stop. |
| `/banana help` | Show this list. |

Only people with write access to both the issue repository and the target repository can use commands.
BananaVibe opens draft pull requests after the configured checks pass; it never merges them.
"""


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""


def parse(body):
    """Return the Command a comment asks for, or None if it is not a command."""
    match = PATTERN.fullmatch(body or "")
    if not match:
        return None
    name = (match.group(1) or "help").lower()
    name = ALIASES.get(name, name)
    argument = (match.group(2) or "").strip()
    if name not in NAMES or (name in NEEDS_ARGUMENT) != bool(argument):
        return Command("help")
    return Command(name, argument[:32000])
