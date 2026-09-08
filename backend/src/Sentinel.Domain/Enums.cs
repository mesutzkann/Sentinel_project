namespace Sentinel.Domain;

public enum UserRole
{
    /// <summary>Read-only access to incidents, investigations and evaluation results.</summary>
    Viewer,

    /// <summary>Can create incidents, start investigations and approve remediations.</summary>
    Engineer,

    /// <summary>Everything an engineer can do, plus user and service management.</summary>
    Admin,
}

public enum IncidentSeverity
{
    Low,
    Medium,
    High,
    Critical,
}

public enum IncidentStatus
{
    Open,
    Investigating,
    AwaitingApproval,
    Resolving,
    Resolved,
    Closed,
}

public enum InvestigationStatus
{
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
}

/// <summary>Where a piece of evidence came from. Drives the source-diversity bonus in the confidence score.</summary>
public enum EvidenceSource
{
    Logs,
    Metrics,
    Traces,
    Git,
    Database,
    SourceCode,
    Docker,
    HistoricalIncident,
    RagDocument,
}

public enum RecommendationStatus
{
    PendingApproval,
    Approved,
    Rejected,
    Executing,
    Executed,

    /// <summary>Executed and confirmed by re-measuring the signal that triggered the incident.</summary>
    Verified,

    Failed,
}

/// <summary>What a model call was for. Lets the evaluation dashboard separate routing cost from reasoning cost.</summary>
public enum ModelPurpose
{
    Routing,
    Reasoning,
    Validation,
    Postmortem,
}

public enum EvaluationKind
{
    Router,
    Rag,
    Agent,
}
