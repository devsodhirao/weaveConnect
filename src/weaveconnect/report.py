"""Check results with three outcomes, so "could not check" never reads as a pass."""

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNCHECKED = "could not check"


EXIT_CODES = {Status.PASS: 0, Status.FAIL: 1, Status.UNCHECKED: 2}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass
class Report:
    title: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str) -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        return check

    @property
    def status(self) -> Status:
        # A report with no checks proved nothing, so it cannot pass.
        if not self.checks:
            return Status.UNCHECKED
        statuses = {check.status for check in self.checks}
        if Status.FAIL in statuses:
            return Status.FAIL
        if Status.UNCHECKED in statuses:
            return Status.UNCHECKED
        return Status.PASS

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def render(self) -> str:
        width = max((len(c.status.value) for c in self.checks), default=0)
        lines = [self.title, ""]
        lines += [f"  {c.status.value.upper():<{width}}  {c.name}: {c.detail}" for c in self.checks]
        if self.notes:
            lines += ["", *[f"  {note}" for note in self.notes]]
        summary = {
            Status.PASS: "Every check passed.",
            Status.FAIL: "At least one check failed. Fix the cause named above and run it again.",
            Status.UNCHECKED: "Some checks could not run, so this proves nothing yet.",
        }[self.status]
        lines += ["", summary]
        return "\n".join(lines)
