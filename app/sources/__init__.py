class SourceError(Exception):
    """An upstream source could not be read or returned an unexpected page.

    `cost` is what a reader charged before the page was rejected (0 when
    nothing was spent), so rejected reads still count toward usage."""

    def __init__(self, message: str, cost: int = 0):
        super().__init__(message)
        self.cost = cost
