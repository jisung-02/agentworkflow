from workflow_engine.application.dto import ChannelCommandDTO


class AllowListPolicy:
    def __init__(self, actors: frozenset[str]) -> None:
        self.actors = actors

    def permits(self, command: ChannelCommandDTO) -> bool:
        return f"{command.source}:{command.actor_id}" in self.actors
