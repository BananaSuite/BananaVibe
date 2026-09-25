"""Only an anchored command is executable; ordinary issue text stays task input."""

from dataclasses import dataclass
import re


HELP = """BananaVibe prepares maintenance drafts for human review.

| Command | Effect |
| --- | --- |
| `/banana help` | Show this list. |
| `/banana start` | Accept this issue and begin a task. |
| `/banana status` | Show its state, checkpoint, and recovery instructions. |
| `/banana stop` | Stop work and save a checkpoint; keep the issue open. |
| `/banana resume` | Continue from the last saved branch. |
| `/banana restart` | Start again from the configured base on a new branch. |
| `/banana models` | List configured model aliases. |
| `/banana model ALIAS` | Change model; a live task continues with that model. |
| `/banana answer TEXT` | Supply requested guidance and resume work. |
| `/banana fail REASON` | Mark the task failed and stop work. |

Only maintainers of both the issue repository and target repository can control tasks.
Passing configured checks produces a draft PR for review; BananaVibe never merges it.
"""


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""


def parse(body):
    match = re.fullmatch(r"\s*/(?:banana|bananavibe)(?:\s+([a-z]+))?(?:[ \t]+([\s\S]*))?\s*", body or "")
    if not match:
        return None
    name = match.group(1) or "help"
    argument = (match.group(2) or "").strip()
    if name not in {"help", "start", "status", "stop", "resume", "restart", "models", "model", "answer", "fail"}:
        return Command("help")
    if name in {"model", "answer", "fail"} and not argument:
        return Command("help")
    if name not in {"model", "answer", "fail"} and argument:
        return Command("help")
    return Command(name, argument[:32000])
