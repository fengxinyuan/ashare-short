"""Brief rendering and delivery channels."""

from ashare_decision_support.notify.base import Notifier, NotifierCapabilities, split_utf8_text
from ashare_decision_support.notify.local import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.render import (
    render_post_close_brief,
    render_pre_open_brief,
    render_sector_brief,
)

__all__ = [
    "ConsoleNotifier",
    "FileNotifier",
    "Notifier",
    "NotifierCapabilities",
    "render_post_close_brief",
    "render_pre_open_brief",
    "render_sector_brief",
    "split_utf8_text",
]
