"""Timer use case; schedule storage and calendar math are consumer-owned ports."""

from workflow_engine.application.ports import Clock, NextFire, ScheduleStore, WorkflowStore
from workflow_engine.domain.model import Definition
from workflow_engine.domain.value_objects import RunId


class TimerService:
    def __init__(
        self,
        definitions: WorkflowStore,
        schedules: ScheduleStore,
        next_fire: NextFire,
        clock: Clock,
    ) -> None:
        self.definitions = definitions
        self.schedules = schedules
        self.next_fire = next_fire
        self.clock = clock

    def register(self, definition: Definition) -> int:
        digest = self.definitions.save_definition(definition)
        now = self.clock.now_epoch()
        for index, timer in enumerate(definition.timers):
            due = self.next_fire.after(timer.cron, timer.timezone, now)
            self.schedules.register_timer(digest, index, timer, due)
        return len(definition.timers)

    def fire_due(self) -> tuple[RunId, ...]:
        now = self.clock.now_epoch()
        created: list[RunId] = []
        for schedule in self.schedules.due_schedules(now):
            next_at = self.next_fire.after(schedule.cron, schedule.timezone, now)
            run_id = self.schedules.fire_schedule(schedule, next_at)
            if run_id is not None:
                created.append(run_id)
        return tuple(created)
