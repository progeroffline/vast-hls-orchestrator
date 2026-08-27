"""Exception hierarchy for Vast.ai API and pipeline failures."""


class VastError(RuntimeError):
    pass


class VastAuthError(VastError):
    pass


class OfferUnavailable(VastError):
    pass


class AmbiguousCreate(VastError):
    pass
