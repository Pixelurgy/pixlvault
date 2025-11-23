from .worker_registry import BaseWorker, WorkerType


class FacialFeaturesWorker(BaseWorker):
    def __init__(self):
        super().__init__()

    def worker_type(self) -> WorkerType:
        return WorkerType.FACIAL_FEATURES

    def _run(self):
        pass
