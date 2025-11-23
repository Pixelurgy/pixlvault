from worker_registry import BaseWorker, WorkerType


class QualityWorker(BaseWorker):
    def __init__(self):
        super().__init__()

    def worker_type(self) -> WorkerType:
        return WorkerType.QUALITY

    def _run(self):
        pass
