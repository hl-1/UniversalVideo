class ModelFault(RuntimeError):
    """A paid-model failure must never become a recoverable music-source error."""

    agent_policy_error = True


class ModelOutputError(ModelFault):
    pass
