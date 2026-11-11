"""Execute neutral Prosaic artifacts with explicitly granted authority."""
__version__ = '0.9.22'

from .artifacts import ProsaicArtifact
from .config import EndpointConfig, RuntimeConfig, RunLimits, CliSandboxConfig
from .policy import RunPolicy
from .runtime import ProsaicRuntime
from .types import Result
from .tools import CustomTool, validate_custom_tools, custom_descriptors
from .terminal import TerminalCompletion
from .admission import validate_execution_artifact
from .operation_context import InvocationScope, ToolExecutionContext
from .tool_effects import ToolFailure, ToolClaim, ToolJournal, tool_journal_descriptor
from .telemetry import ObserverEmitter
from .conformance import evaluate_conformance

__all__ = ["ProsaicArtifact", "EndpointConfig", "RuntimeConfig", "RunLimits", "CliSandboxConfig", "RunPolicy", "ProsaicRuntime", "Result", "CustomTool"]
__all__ += ["validate_execution_artifact", "validate_custom_tools", "custom_descriptors", "TerminalCompletion"]

from .tool_contracts import (ToolContractLimitsV1, EvidenceDraftV1, ToolResultV1,
    ToolResultContractV1, ToolDisplayFieldV1, ToolEventPolicyV1, EvidenceUseV1,
    ToolDisplayUseV1, EvidenceAuthorizerV1, ToolDisplayAuthorizerV1,
    CitationPolicyV1, ToolContractBindingsV1)
__all__ += ['ToolContractLimitsV1', 'EvidenceDraftV1', 'ToolResultV1',
    'ToolResultContractV1', 'ToolDisplayFieldV1', 'ToolEventPolicyV1', 'EvidenceUseV1',
    'ToolDisplayUseV1', 'EvidenceAuthorizerV1', 'ToolDisplayAuthorizerV1',
    'CitationPolicyV1', 'ToolContractBindingsV1']

from .structured_tools import Completion, StructuredToolError, StructuredToolLoop, ToolRequest

__all__ += ["Completion", "StructuredToolError", "StructuredToolLoop", "ToolRequest"]

from .accounting import AccountingError, ExecutionContext, ResolvedContext, RateCard
__all__ += ["AccountingError", "ExecutionContext", "ResolvedContext", "RateCard"]
__all__ += ["InvocationScope", "ObserverEmitter"]
__all__ += ["ToolExecutionContext", "ToolClaim", "ToolJournal", "tool_journal_descriptor"]
__all__ += ["evaluate_conformance"]

from .conversation import (ConversationMessage, CompletedTurn, TurnEvidence,
                           TurnResult, TurnLimits, PreparedTurn, TurnAdmissionError)
__all__ += ["ConversationMessage", "CompletedTurn", "TurnEvidence", "TurnResult",
            "TurnLimits", "PreparedTurn", "TurnAdmissionError"]

from .adapter import RuntimeAdapter
__all__ += ["RuntimeAdapter"]

from .execution_budget import ExecutionBudget
__all__ += ["ExecutionBudget"]

from .context_contracts import (ContextPolicy, TokenCounterDescriptor,
    ContextAssemblyReport, ContextSelection, ContextWorkState)
from .context_counters import (TokenCounter, TokenCounterRegistration,
    TokenCounterRegistry)
__all__ += ["ContextPolicy", "TokenCounterDescriptor", "ContextAssemblyReport",
    "ContextSelection", "ContextWorkState", "TokenCounter", "TokenCounterRegistration",
    "TokenCounterRegistry"]

from .lifecycle import ExecutionLifecycle, Draining
__all__ += ["ExecutionLifecycle", "Draining"]

__all__ += ["ToolFailure"]
from .tool_outcomes import ToolOutcome
__all__ += ["ToolOutcome"]
from .native_config import NativeProfile, ProfilePool, NativePolicy, NativeRuntimeConfig
from .capacity import CapacityStore
__all__ += ["NativeProfile", "ProfilePool", "NativePolicy", "NativeRuntimeConfig", "CapacityStore"]

from .native import NativeRuntime
__all__ += ["NativeRuntime"]

from .input_views import AuthoritativeInputLimits
__all__ += ['AuthoritativeInputLimits']
from .frozen_turns import FrozenTurnInput, InputVariantSelection
__all__ += ['FrozenTurnInput', 'InputVariantSelection']

from .rules import (Guard, GuardContext, RuleAdmissionError, RuleBindingWarning, RuleSet, Verdict,
                    compile_rules, rules_fingerprint)
from .guards import generic_guards
__all__ += ["Guard", "GuardContext", "RuleAdmissionError", "RuleBindingWarning", "RuleSet", "Verdict",
            "compile_rules", "rules_fingerprint", "generic_guards"]

from .unified_config import PoolPolicy, UnifiedRuntimeConfig
from .unified import UnifiedRuntime
__all__ += ["PoolPolicy", "UnifiedRuntimeConfig", "UnifiedRuntime"]
from .citations import CitationsV1, CitationViolation, validate_citations
from .tool_evidence import ToolInvocationEvidenceV1, EligibleEvidenceV1
from .tool_evidence import HostDeliveryAttestationV1
from .tool_imports import HostEvidenceImportV1
__all__ += ['CitationsV1', 'CitationViolation', 'validate_citations', 'ToolInvocationEvidenceV1',
            'EligibleEvidenceV1', 'HostDeliveryAttestationV1', 'HostEvidenceImportV1']

from .tool_parallel_contracts import ParallelToolPolicyV1
__all__ += ['ParallelToolPolicyV1']
