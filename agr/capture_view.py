"""Read one run at a saved capture while other runs retain their latest view."""
from .imports import ImportProblem


class PinnedStore:
    def __init__(self, store, run_id, capture_id):
        if not any(c["capture_id"] == capture_id for c in store.read_index(run_id)):
            raise ImportProblem("capture_not_found", "The saved capture is no longer available for this run.", 404)
        self.store, self.run_id, self.capture_id = store, run_id, capture_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    def latest_capture_id(self, run_id):
        return self.capture_id if run_id == self.run_id else self.store.latest_capture_id(run_id)
