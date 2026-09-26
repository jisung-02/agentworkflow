from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("run id must not be empty")


@dataclass(frozen=True, slots=True)
class TokenId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("token id must not be empty")
