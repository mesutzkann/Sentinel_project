namespace Sentinel.Domain.Entities;

/// <summary>
/// One MCP tool invocation. Together these make the agent's own behaviour measurable: tool
/// latency, tool coverage per scenario, and the audit trail for destructive calls.
/// </summary>
public sealed class ToolCall
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public Guid? StepId { get; set; }

    public InvestigationStep? Step { get; set; }

    /// <summary>MCP server that owns the tool, e.g. <c>logs-mcp</c>.</summary>
    public required string Server { get; set; }

    public required string Tool { get; set; }

    public string? Args { get; set; }

    public string? ResultSummary { get; set; }

    public bool Success { get; set; }

    public int LatencyMs { get; set; }

    public bool IsDestructive { get; set; }

    /// <summary>
    /// The approved recommendation that authorised this call. Must be set whenever
    /// <see cref="IsDestructive"/> is true — an unauthorised destructive call is a bug.
    /// </summary>
    public Guid? ApprovalId { get; set; }

    public Recommendation? Approval { get; set; }

    public DateTimeOffset CalledAt { get; set; } = DateTimeOffset.UtcNow;
}

/// <summary>
/// One LLM call, with its cost and whether it produced valid JSON.
/// </summary>
/// <remarks>
/// <see cref="ValidJson"/> exists because invalid-JSON rate is a headline metric of the
/// fine-tuned router: the whole point of Phase 8 is showing a small tuned model beating a base
/// model at emitting a schema reliably.
/// </remarks>
public sealed class ModelPrediction
{
    public Guid Id { get; set; } = Guid.NewGuid();

    /// <summary>Null for calls outside an investigation, such as an evaluation run.</summary>
    public Guid? InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public required string ModelName { get; set; }

    public ModelPurpose Purpose { get; set; }

    public int PromptTokens { get; set; }

    public int CompletionTokens { get; set; }

    public int LatencyMs { get; set; }

    public bool ValidJson { get; set; }

    public string? Output { get; set; }

    public DateTimeOffset CalledAt { get; set; } = DateTimeOffset.UtcNow;
}

/// <summary>One benchmark execution: a model or configuration measured against a fixed test set.</summary>
public sealed class EvaluationRun
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public EvaluationKind Kind { get; set; }

    /// <summary>What was measured, e.g. <c>sentinel-router-v1</c> or <c>hybrid+rerank</c>.</summary>
    public required string ModelOrConfig { get; set; }

    public DateTimeOffset StartedAt { get; set; } = DateTimeOffset.UtcNow;

    public DateTimeOffset? CompletedAt { get; set; }

    /// <summary>Aggregate metrics. Shape depends on <see cref="Kind"/>; the frontend charts these directly.</summary>
    public string? Metrics { get; set; }

    public string? Notes { get; set; }

    public List<EvaluationResult> Results { get; set; } = [];
}

/// <summary>One test case within a run, kept so a regression can be traced to the case that caused it.</summary>
public sealed class EvaluationResult
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid RunId { get; set; }

    public EvaluationRun? Run { get; set; }

    public required string CaseId { get; set; }

    public string? Expected { get; set; }

    public string? Actual { get; set; }

    public bool Passed { get; set; }

    public string? Details { get; set; }
}
