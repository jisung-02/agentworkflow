from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO


class EchoRunner:
    """Deterministic runner for examples, development, and installation checks."""

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        text = request.instructions.strip()
        if request.inputs:
            text += "\n" + "\n".join(f"{key}={value}" for key, value in request.inputs)
        return TaskResultDTO(outcome=request.outcomes[0], output=text)
