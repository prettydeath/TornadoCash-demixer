"""Exception types raised by the package.

Everything here derives from :class:`TornadoDemixError`, an ordinary
``Exception``, so an embedder (the web UI, a notebook) can catch it.
:mod:`tornado_demix.cli` is the one place that turns these into ``SystemExit``.
"""


class TornadoDemixError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(TornadoDemixError):
    """A configuration file is missing, unreadable, or has no usable content."""


class RegistryError(TornadoDemixError):
    """The pool registry is missing a network, or holds an unusable row."""


class ApiError(TornadoDemixError):
    """The data provider did not answer.

    Distinct from an empty result: "the provider never answered" must not turn
    into "this wallet made no Tornado deposits".
    """


class PermanentApiError(ApiError):
    """The provider refused the request in a way that retrying cannot fix.

    Examples: the chain is not covered by the plan ("Free API access is not
    supported for this chain"), or the address is malformed. Raised on the first
    answer, without the backoff a transient error gets.
    """


class ProviderBlockedError(PermanentApiError):
    """The provider answered with a bot-protection challenge (Cloudflare) instead of data.

    Never worked around: the run needs a paid plan or another endpoint.
    """


class ArchiveUnavailableError(PermanentApiError):
    """A JSON-RPC node refused a historical block range (it keeps recent blocks only).

    Retrying or halving the span cannot help; the run needs an archive endpoint or an
    explorer plan that serves the chain.
    """


class ApiKeyError(ApiError):
    """The provider rejected the API key, or no key was supplied."""


class BlockLookupError(TornadoDemixError):
    """A timestamp could not be resolved to a block number.

    An exception rather than ``None``: a caller that fell back to "the whole
    chain" would drop the timing correlation the analysis depends on while still
    producing confident output.
    """
