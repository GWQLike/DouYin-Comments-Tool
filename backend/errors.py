"""Shared worker exceptions, independent of collection and CLI modules."""


class StopRequested(Exception):
    pass


class ControlledStop(StopRequested):
    """A locally authored control reason safe to show in the workbench."""
    def __init__(self, reason, *, kind=None):
        super().__init__()
        self.public_reason = reason
        self.kind = kind


class PageFailure(Exception):
    pass


class ManualActionRequired(Exception):
    pass
