"""Console and local-file notifiers."""

from __future__ import annotations

from pathlib import Path

from ashare_decision_support.notify.base import Notifier, NotifierCapabilities
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.state.store import write_json, write_text


class ConsoleNotifier(Notifier):
    capabilities = NotifierCapabilities(channel="console", markdown=True, max_bytes=1_000_000)

    def send(self, title: str, content: str) -> bool:
        print(f"\n===== {title} =====")
        print(content)
        return True


class FileNotifier(Notifier):
    """Writes the brief to `<output_dir>/<slug>-<trade_date>.md`.

    The markdown file is the deliverable a human reads. `write_payload` is the
    machine-readable sibling used by the next run (pre-open reads what post-close
    wrote).
    """

    capabilities = NotifierCapabilities(channel="file", markdown=True, max_bytes=10_000_000)

    def __init__(
        self, output_dir: str | Path, slug: str = "brief", stamp: str | None = None
    ) -> None:
        self.output_dir = Path(output_dir)
        self.slug = slug
        self.stamp = stamp or beijing_naive_now().strftime("%Y%m%d")
        self.last_path: Path | None = None

    def send(self, title: str, content: str) -> bool:
        self.last_path = None
        try:
            path = self.output_dir / f"{self.slug}-{self.stamp}.md"
            write_text(path, f"# {title}\n\n{content}\n")
            self.last_path = path
            return True
        except OSError as exc:
            print(f"[file] write failed: {exc}")
            return False

    def write_payload(self, payload: object, name: str) -> Path | None:
        """Write a JSON sibling. Returns the path, or None if writing failed."""
        try:
            path = self.output_dir / name
            write_json(path, payload)
            return path
        except (OSError, TypeError, ValueError) as exc:
            print(f"[file] write failed: {exc}")
            return None
