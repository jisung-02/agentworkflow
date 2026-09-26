from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


class CronNextFire:
    def after(self, cron: str, timezone: str, epoch: float) -> float:
        base = datetime.fromtimestamp(epoch, ZoneInfo(timezone))
        return croniter(cron, base).get_next(datetime).timestamp()
