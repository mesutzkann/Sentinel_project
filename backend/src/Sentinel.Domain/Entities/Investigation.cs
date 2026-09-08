namespace Sentinel.Domain.Entities;

/// <summary>
/// One run of the agent against an incident.
/// </summary>
/// <remarks>
/// Created by the backend, driven by the AI service. Every field below the status is written
/// from callback events (ADR-0002), which is why they are all nullable or defaulted: a running
/// investigation has not filled them in yet, and a failed one never will.
/// </remarks>
public sealed class Investigation
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid IncidentId { get; set; }

    public Incident? Incident { get; set; }

    /// <summary>The natural-language question the agent was asked.</summary>
    public required string Query { get; set; }

    /// <summary>Intent the router assigned, e.g. <c>LOG_QUERY</c>. Null until routing completes.</summary>
    public string? RouterIntent { get; set; }

    /// <summary>Full router output, kept verbatim so router evaluation can be re-run offline.</summary>
    public string? RouterOutput { get; set; }

    public InvestigationStatus Status { get; set; } = InvestigationStatus.Queued;

    public DateTimeOffset StartedAt { get; set; } = DateTimeOffset.UtcNow;

    public DateTimeOffset? CompletedAt { get; set; }

    public int? TotalDurationMs { get; set; }

    public int LlmCalls { get; set; }

    public int ToolCalls { get; set; }

    public int PromptTokens { get; set; }

    public int CompletionTokens { get; set; }

    /// <summary>
    /// Why the run ended without a result. Set for <see cref="InvestigationStatus.Failed"/>, and
    /// also when confidence fell below the threshold and the run stopped for a human.
    /// </summary>
    public string? FailureReason { get; set; }

    public List<InvestigationStep> Steps { get; set; } = [];

    public List<Evidence> Evidence { get; set; } = [];

    public List<Hypothesis> Hypotheses { get; set; } = [];

    public List<RootCause> RootCauses { get; set; } = [];

    public List<ToolCall> ToolCallRecords { get; set; } = [];
}

/// <summary>One state transition in the agent's state machine, as it happened.</summary>
public sealed class InvestigationStep
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    /// <summary>
    /// Monotonic per investigation. Lets the frontend order steps deterministically and lets the
    /// backend notice a gap when a callback is lost.
    /// </summary>
    public int Sequence { get; set; }

    /// <summary>State machine state, e.g. <c>COLLECT_LOGS</c>.</summary>
    public required string State { get; set; }

    /// <summary>Short human-readable line for the timeline, e.g. "347 NullReferenceException found".</summary>
    public required string Message { get; set; }

    /// <summary>State-specific detail, shape depends on <see cref="State"/>.</summary>
    public string? Payload { get; set; }

    public int? DurationMs { get; set; }

    public DateTimeOffset StartedAt { get; set; } = DateTimeOffset.UtcNow;

    public DateTimeOffset? CompletedAt { get; set; }
}

/// <summary>A fact the agent collected, with the weight it carried in the conclusion.</summary>
public sealed class Evidence
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public Guid? StepId { get; set; }

    public InvestigationStep? Step { get; set; }

    public EvidenceSource Source { get; set; }

    public required string Summary { get; set; }

    /// <summary>The underlying tool output, so a conclusion can always be traced back to raw data.</summary>
    public string? Raw { get; set; }

    /// <summary>0.00 to 1.00. Feeds the evidence-support term of the confidence score.</summary>
    public decimal Weight { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}

/// <summary>A candidate explanation, scored and ranked against its siblings.</summary>
public sealed class Hypothesis
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public required string Title { get; set; }

    public string? Description { get; set; }

    /// <summary>0.00 to 1.00. The gap between rank 1 and rank 2 is the margin term of the confidence score.</summary>
    public decimal Score { get; set; }

    public int Rank { get; set; }

    public bool IsSelected { get; set; }
}

/// <summary>The explanation the agent settled on, after the critic had its say.</summary>
public sealed class RootCause
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public Guid? HypothesisId { get; set; }

    public Hypothesis? Hypothesis { get; set; }

    public required string Title { get; set; }

    /// <summary>
    /// One of the 15 chaos scenario codes, e.g. <c>DB_CONNECTION_POOL_EXHAUSTION</c>. Agent
    /// evaluation compares this against the scenario that was actually triggered, so the values
    /// must stay stable once evaluation data exists.
    /// </summary>
    public required string Category { get; set; }

    /// <summary>0.00 to 1.00. Below 0.70 the agent stops and asks for a human instead of recommending.</summary>
    public decimal Confidence { get; set; }

    public string? Explanation { get; set; }

    /// <summary>What the critic checked and concluded.</summary>
    public string? ValidatorOutput { get; set; }

    public decimal? ValidatorConfidence { get; set; }

    public List<Recommendation> Recommendations { get; set; } = [];
}
