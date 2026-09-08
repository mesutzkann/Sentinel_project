namespace Sentinel.Domain.Entities;

/// <summary>
/// A proposed fix, and the record of what happened when a human approved it.
/// </summary>
/// <remarks>
/// The approval gate of the whole system. A destructive MCP tool refuses to run without an
/// approval token minted from an approved row here, so this table is the audit trail for every
/// change the agent made to a running system.
/// </remarks>
public sealed class Recommendation
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid InvestigationId { get; set; }

    public Investigation? Investigation { get; set; }

    public Guid RootCauseId { get; set; }

    public RootCause? RootCause { get; set; }

    /// <summary>Stable action identifier, e.g. <c>RESTORE_POOL_SIZE</c>.</summary>
    public required string ActionCode { get; set; }

    public required string Description { get; set; }

    /// <summary>MCP tool that carries this out, e.g. <c>update_env_and_restart</c>.</summary>
    public string? ToolName { get; set; }

    /// <summary>Arguments for that tool, exactly as they will be sent.</summary>
    public string? ToolArgs { get; set; }

    /// <summary>
    /// False only for advisory recommendations with no tool behind them. Every recommendation
    /// that touches a running service requires approval.
    /// </summary>
    public bool RequiresApproval { get; set; } = true;

    public RecommendationStatus Status { get; set; } = RecommendationStatus.PendingApproval;

    public Guid? ApprovedBy { get; set; }

    public User? Approver { get; set; }

    public DateTimeOffset? ApprovedAt { get; set; }

    public DateTimeOffset? ExecutedAt { get; set; }

    /// <summary>What the tool returned.</summary>
    public string? ExecutionResult { get; set; }

    /// <summary>
    /// Whether the fix actually worked, measured by re-running the signal that triggered the
    /// incident. A recommendation reaches <see cref="RecommendationStatus.Verified"/> only when
    /// this says so.
    /// </summary>
    public string? VerificationResult { get; set; }
}

/// <summary>
/// The writeup produced after resolution, and the seed of the system's memory: it is ingested
/// into the RAG store so the next similar incident retrieves it as evidence.
/// </summary>
public sealed class Postmortem
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid IncidentId { get; set; }

    public Incident? Incident { get; set; }

    public required string ContentMarkdown { get; set; }

    /// <summary>Machine-readable version of the same content, for retrieval metadata and filters.</summary>
    public string? Structured { get; set; }

    public int? DurationMinutes { get; set; }

    public string? LessonsLearned { get; set; }

    public string? ChangedFiles { get; set; }

    public string? RelevantCommits { get; set; }

    /// <summary>
    /// False until the AI service confirms the document reached the chunk store. Kept so a failed
    /// ingest can be retried instead of silently losing the memory.
    /// </summary>
    public bool IngestedToRag { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}
