"""Invocation-local allowances; counts are dispatch attempts, not estimates."""


class InvocationBudgetExceeded(Exception):
    REASONS = frozenset({'provider_request_limit', 'tool_call_limit', 'token_limit', 'usage_unknown'})

    def __init__(self, reason):
        if reason not in self.REASONS:
            raise ValueError('unknown invocation budget reason')
        self.reason = reason
        super().__init__(reason)


class InvocationBudget:
    def __init__(self, policy):
        self.policy = policy
        self.provider_requests = self.tool_calls = self.reported_tokens = 0
        self.usage_complete = True

    @property
    def enabled(self):
        return any(value is not None for value in (self.policy.max_provider_requests,
            self.policy.max_tool_calls, self.policy.max_reported_tokens))

    def metadata(self):
        return {'provider_requests': self.provider_requests, 'tool_calls': self.tool_calls,
                'reported_tokens': self.reported_tokens, 'usage_complete': self.usage_complete}

    def _check_reported_allowance(self):
        self.validate_terminal_usage()
        if (self.policy.max_reported_tokens is not None and
                self.reported_tokens >= self.policy.max_reported_tokens):
            raise InvocationBudgetExceeded('token_limit')

    def _reserve(self, name, cap, reason):
        if cap is not None and getattr(self, name) >= cap:
            raise InvocationBudgetExceeded(reason)
        setattr(self, name, getattr(self, name) + 1)

    def before_provider(self):
        self._check_reported_allowance()
        self._reserve('provider_requests', self.policy.max_provider_requests, 'provider_request_limit')

    def before_tool(self):
        self._check_reported_allowance()
        self._reserve('tool_calls', self.policy.max_tool_calls, 'tool_call_limit')

    def record_usage(self, usage):
        if type(usage) is not int or usage < 0:
            self.usage_complete = False
        else:
            self.reported_tokens += usage
        self.validate_terminal_usage()

    def validate_terminal_usage(self):
        cap = self.policy.max_reported_tokens
        if cap is not None:
            if not self.usage_complete:
                raise InvocationBudgetExceeded('usage_unknown')
            if self.reported_tokens > cap:
                raise InvocationBudgetExceeded('token_limit')
