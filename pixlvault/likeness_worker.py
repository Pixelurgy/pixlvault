from worker_registry import BaseWorker, WorkerType


class LikenessWorker(BaseWorker):
    def __init__(self):
        super().__init__()

    def worker_type(self) -> WorkerType:
        return WorkerType.LIKENESS

    def _run(self):
        pass
