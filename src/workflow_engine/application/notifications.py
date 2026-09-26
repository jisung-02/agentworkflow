"""Deliver durable channel notifications through consumer-owned sender ports."""

from collections.abc import Mapping

from workflow_engine.application.ports import NotificationSender, NotificationStore


class NotificationService:
    def __init__(self, store: NotificationStore, senders: Mapping[str, NotificationSender]) -> None:
        self.store = store
        self.senders = senders

    def deliver_pending(self) -> int:
        sent = 0
        for notification in self.store.pending_notifications():
            sender = self.senders.get(notification.source)
            if sender is None:
                continue
            try:
                sender.send(notification)
            except (OSError, RuntimeError):
                continue
            if self.store.mark_notification_sent(notification.id):
                sent += 1
        return sent
