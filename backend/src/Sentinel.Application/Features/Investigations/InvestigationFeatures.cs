using System.Text.Json;
using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.Investigations;

// --------------------------------------------------------------------------- dtos ----

/// <summary>One investigation, without the detail a timeline needs.</summary>
public sealed record InvestigationDto(
    Guid Id,
    Guid IncidentId,
    string IncidentCode,
    string Query,
    string? RouterIntent,
    InvestigationStatus Status,
    DateTimeOffset StartedAt,
    DateTimeOffset? CompletedAt,
    int? TotalDurationMs,
    int LlmCalls,
    int ToolCalls,
    int PromptTokens,
    int CompletionTokens,
    string? FailureReason,
    string? RootCauseTitle,
    string? RootCauseCategory,
    decimal? Confidence,
    int StepCount,
    int EvidenceCount);

public sealed record InvestigationStepDto(
    Guid Id,
    int Sequence,
    string State,
    string Message,
    JsonElement? Payload,
    int? DurationMs,
    DateTimeOffset StartedAt,
    DateTimeOffset? CompletedAt);

public sealed record EvidenceDto(
    Guid Id,
    Guid? StepId,
    EvidenceSource Source,
    string Summary,
    decimal Weight,
    JsonElement? Raw,
    DateTimeOffset CreatedAt);

public sealed record HypothesisDto(
    Guid Id,
    string Title,
    string? Description,
    decimal Score,
    int Rank,
    bool IsSelected);

public sealed record RootCauseDto(
    Guid Id,
    Guid? HypothesisId,
    string Title,
    string Category,
    decimal Confidence,
    string? Explanation,
    JsonElement? ValidatorOutput,
    decimal? ValidatorConfidence);

public sealed record RecommendationDto(
    Guid Id,
    Guid RootCauseId,
    string ActionCode,
    string Description,
    string? ToolName,
    JsonElement? ToolArgs,
    bool RequiresApproval,
    RecommendationStatus Status);

public sealed record ToolCallDto(
    Guid Id,
    Guid? StepId,
    string Server,
    string Tool,
    string? ResultSummary,
    bool Success,
    int LatencyMs,
    DateTimeOffset CalledAt);

/// <summary>Everything one investigation produced. What the investigation screen reads.</summary>
public sealed record InvestigationDetailDto(
    InvestigationDto Investigation,
    IReadOnlyList<InvestigationStepDto> Steps,
    IReadOnlyList<EvidenceDto> Evidence,
    IReadOnlyList<HypothesisDto> Hypotheses,
    RootCauseDto? RootCause,
    IReadOnlyList<RecommendationDto> Recommendations,
    IReadOnlyList<ToolCallDto> ToolCalls);

/// <summary>
/// Pushes an investigation's progress to the browsers watching it.
/// </summary>
/// <remarks>
/// The application layer declares this and the API implements it over SignalR. That is the only
/// way round that works: the handler writing a callback event to the database is also the thing
/// that knows a step happened, and it must not depend on ASP.NET's hub types to say so.
///
/// Every method is fire-and-forget from the handler's point of view. A browser that missed a
/// push can reload and read the same rows over HTTP; an event that failed to persist because a
/// WebSocket was closing would be a real loss.
/// </remarks>
public interface IInvestigationNotifier
{
    Task StepUpdatedAsync(Guid investigationId, InvestigationStepDto step, CancellationToken cancellationToken = default);

    Task EvidenceAddedAsync(Guid investigationId, EvidenceDto evidence, CancellationToken cancellationToken = default);

    Task HypothesisUpdatedAsync(Guid investigationId, HypothesisDto hypothesis, CancellationToken cancellationToken = default);

    Task RootCauseFoundAsync(Guid investigationId, RootCauseDto rootCause, CancellationToken cancellationToken = default);

    Task InvestigationCompletedAsync(Guid investigationId, InvestigationDto investigation, CancellationToken cancellationToken = default);
}

// ----------------------------------------------------------------------- commands ----

/// <summary>Asks the agent to investigate an incident.</summary>
/// <param name="Query">
/// The question, in the user's own words. Defaulted from the incident when the caller has none:
/// the router reads this, and "why is orders erroring" and "check the logs" produce different
/// plans, so it is worth carrying rather than generating.
/// </param>
public sealed record StartInvestigationCommand(
    Guid IncidentId,
    string? Query = null,
    string? ServiceHint = null) : IRequest<InvestigationDto>;

public sealed class StartInvestigationValidator : AbstractValidator<StartInvestigationCommand>
{
    public StartInvestigationValidator()
    {
        RuleFor(c => c.IncidentId).NotEmpty();
        RuleFor(c => c.Query).MaximumLength(2000);
        RuleFor(c => c.ServiceHint).MaximumLength(100);
    }
}

public sealed class StartInvestigationHandler : IRequestHandler<StartInvestigationCommand, InvestigationDto>
{
    private readonly ISentinelDbContext _db;
    private readonly IAiServiceClient _aiService;
    private readonly ICallbackTokenService _tokens;
    private readonly ILogger<StartInvestigationHandler> _logger;

    public StartInvestigationHandler(
        ISentinelDbContext db,
        IAiServiceClient aiService,
        ICallbackTokenService tokens,
        ILogger<StartInvestigationHandler> logger)
    {
        _db = db;
        _aiService = aiService;
        _tokens = tokens;
        _logger = logger;
    }

    public async Task<InvestigationDto> Handle(
        StartInvestigationCommand request,
        CancellationToken cancellationToken)
    {
        var incident = await _db.Incidents
            .Include(i => i.Service)
            .FirstOrDefaultAsync(i => i.Id == request.IncidentId, cancellationToken)
            ?? throw new NotFoundException(nameof(Incident), request.IncidentId);

        var running = await _db.Investigations.AnyAsync(
            i => i.IncidentId == incident.Id
                && (i.Status == InvestigationStatus.Queued || i.Status == InvestigationStatus.Running),
            cancellationToken);

        if (running)
        {
            // Two agents on one incident would interleave two timelines on the same screen and
            // spend twice the model time to disagree with themselves.
            throw new ConflictException($"Incident {incident.IncidentCode} is already being investigated.");
        }

        var investigation = new Investigation
        {
            IncidentId = incident.Id,
            Query = request.Query ?? DefaultQuery(incident),
            Status = InvestigationStatus.Running,
        };

        _db.Investigations.Add(investigation);

        // Investigating, not Open: the incident's status is what the dashboard sorts by, and an
        // incident with an agent working on it is not waiting for one.
        incident.Status = IncidentStatus.Investigating;

        await _db.SaveChangesAsync(cancellationToken);

        try
        {
            await _aiService.StartInvestigationAsync(
                new StartInvestigationRequest(
                    investigation.Id,
                    incident.IncidentCode,
                    investigation.Query,
                    request.ServiceHint ?? incident.Service?.Name,
                    CallbackUrlFor(investigation.Id),
                    _tokens.Issue(investigation.Id)),
                cancellationToken);
        }
        catch (AiServiceUnavailableException exception)
        {
            // The row stays, and it says what happened. Deleting it would leave a person who
            // pressed Investigate with nothing to look at and no reason why.
            _logger.LogError(exception, "Could not start investigation {InvestigationId}", investigation.Id);

            investigation.Status = InvestigationStatus.Failed;
            investigation.CompletedAt = DateTimeOffset.UtcNow;
            investigation.FailureReason = exception.Message;
            incident.Status = IncidentStatus.Open;

            await _db.SaveChangesAsync(cancellationToken);

            throw;
        }

        return Map(investigation, incident.IncidentCode, rootCause: null, stepCount: 0, evidenceCount: 0);
    }

    /// <summary>
    /// The callback path, which the AI service posts every event to.
    /// </summary>
    /// <remarks>
    /// Relative on purpose. The base URL is the AI service's view of this backend and belongs in
    /// its configuration — inside compose it is a container hostname and natively it is
    /// localhost — so the client that knows which AI service it is talking to is the thing that
    /// prefixes it.
    /// </remarks>
    internal static string CallbackUrlFor(Guid investigationId) =>
        $"/internal/investigations/{investigationId}/events";

    private static string DefaultQuery(Incident incident) =>
        $"{incident.Service?.Name ?? "The service"}: {incident.Title}. Investigate and find the root cause.";

    internal static InvestigationDto Map(
        Investigation investigation,
        string incidentCode,
        RootCause? rootCause,
        int stepCount,
        int evidenceCount) => new(
            investigation.Id,
            investigation.IncidentId,
            incidentCode,
            investigation.Query,
            investigation.RouterIntent,
            investigation.Status,
            investigation.StartedAt,
            investigation.CompletedAt,
            investigation.TotalDurationMs,
            investigation.LlmCalls,
            investigation.ToolCalls,
            investigation.PromptTokens,
            investigation.CompletionTokens,
            investigation.FailureReason,
            rootCause?.Title,
            rootCause?.Category,
            rootCause?.Confidence,
            stepCount,
            evidenceCount);
}

// ------------------------------------------------------------------------ queries ----

/// <param name="IncidentId">Restrict to one incident. Null means every investigation.</param>
public sealed record ListInvestigationsQuery(Guid? IncidentId = null, int Limit = 50)
    : IRequest<IReadOnlyList<InvestigationDto>>;

public sealed class ListInvestigationsHandler
    : IRequestHandler<ListInvestigationsQuery, IReadOnlyList<InvestigationDto>>
{
    private readonly ISentinelDbContext _db;

    public ListInvestigationsHandler(ISentinelDbContext db) => _db = db;

    public async Task<IReadOnlyList<InvestigationDto>> Handle(
        ListInvestigationsQuery request,
        CancellationToken cancellationToken)
    {
        var query = _db.Investigations.AsNoTracking();

        if (request.IncidentId is { } incidentId)
        {
            query = query.Where(i => i.IncidentId == incidentId);
        }

        return await query
            .OrderByDescending(i => i.StartedAt)
            .Take(Math.Clamp(request.Limit, 1, 200))
            .Select(i => new InvestigationDto(
                i.Id,
                i.IncidentId,
                i.Incident!.IncidentCode,
                i.Query,
                i.RouterIntent,
                i.Status,
                i.StartedAt,
                i.CompletedAt,
                i.TotalDurationMs,
                i.LlmCalls,
                i.ToolCalls,
                i.PromptTokens,
                i.CompletionTokens,
                i.FailureReason,
                // The last one written: the critic discards a rejected conclusion, so an
                // investigation has one root cause and a defensive OrderBy costs nothing.
                i.RootCauses.OrderByDescending(r => r.Confidence).Select(r => r.Title).FirstOrDefault(),
                i.RootCauses.OrderByDescending(r => r.Confidence).Select(r => r.Category).FirstOrDefault(),
                i.RootCauses.OrderByDescending(r => r.Confidence).Select(r => (decimal?)r.Confidence).FirstOrDefault(),
                i.Steps.Count,
                i.Evidence.Count))
            .ToListAsync(cancellationToken);
    }
}

public sealed record GetInvestigationQuery(Guid Id) : IRequest<InvestigationDetailDto>;

public sealed class GetInvestigationHandler
    : IRequestHandler<GetInvestigationQuery, InvestigationDetailDto>
{
    private readonly ISentinelDbContext _db;

    public GetInvestigationHandler(ISentinelDbContext db) => _db = db;

    public async Task<InvestigationDetailDto> Handle(
        GetInvestigationQuery request,
        CancellationToken cancellationToken)
    {
        var investigation = await _db.Investigations
            .AsNoTracking()
            .Include(i => i.Incident)
            .Include(i => i.Steps)
            .Include(i => i.Evidence)
            .Include(i => i.Hypotheses)
            .Include(i => i.RootCauses)
                .ThenInclude(r => r.Recommendations)
            .Include(i => i.ToolCallRecords)
            .FirstOrDefaultAsync(i => i.Id == request.Id, cancellationToken)
            ?? throw new NotFoundException(nameof(Investigation), request.Id);

        // The jsonb columns are parsed here rather than in the projection: EF translates the
        // query into SQL and cannot call a parser inside it, and the alternative — handing the
        // frontend a JSON document wrapped in a JSON string — makes every consumer parse twice.
        var rootCause = investigation.RootCauses
            .OrderByDescending(r => r.Confidence)
            .FirstOrDefault();

        return new InvestigationDetailDto(
            StartInvestigationHandler.Map(
                investigation,
                investigation.Incident!.IncidentCode,
                rootCause,
                investigation.Steps.Count,
                investigation.Evidence.Count),
            investigation.Steps
                .OrderBy(s => s.Sequence)
                .Select(s => new InvestigationStepDto(
                    s.Id,
                    s.Sequence,
                    s.State,
                    s.Message,
                    Json.Parse(s.Payload),
                    s.DurationMs,
                    s.StartedAt,
                    s.CompletedAt))
                .ToList(),
            investigation.Evidence
                .OrderBy(e => e.CreatedAt)
                .Select(e => new EvidenceDto(
                    e.Id,
                    e.StepId,
                    e.Source,
                    e.Summary,
                    e.Weight,
                    Json.Parse(e.Raw),
                    e.CreatedAt))
                .ToList(),
            investigation.Hypotheses
                .OrderBy(h => h.Rank == 0 ? int.MaxValue : h.Rank)
                .ThenByDescending(h => h.Score)
                .Select(h => new HypothesisDto(h.Id, h.Title, h.Description, h.Score, h.Rank, h.IsSelected))
                .ToList(),
            rootCause is null ? null : new RootCauseDto(
                rootCause.Id,
                rootCause.HypothesisId,
                rootCause.Title,
                rootCause.Category,
                rootCause.Confidence,
                rootCause.Explanation,
                Json.Parse(rootCause.ValidatorOutput),
                rootCause.ValidatorConfidence),
            (rootCause?.Recommendations ?? [])
                .Select(r => new RecommendationDto(
                    r.Id,
                    r.RootCauseId,
                    r.ActionCode,
                    r.Description,
                    r.ToolName,
                    Json.Parse(r.ToolArgs),
                    r.RequiresApproval,
                    r.Status))
                .ToList(),
            investigation.ToolCallRecords
                .OrderBy(t => t.CalledAt)
                .Select(t => new ToolCallDto(
                    t.Id,
                    t.StepId,
                    t.Server,
                    t.Tool,
                    t.ResultSummary,
                    t.Success,
                    t.LatencyMs,
                    t.CalledAt))
                .ToList());
    }
}

/// <summary>Reading and writing the jsonb columns, in one place so they round-trip the same way.</summary>
internal static class Json
{
    /// <summary>A stored document as an element, or null when the column is empty.</summary>
    /// <remarks>
    /// Never throws. These columns are written by the AI service, and a row that cannot be
    /// parsed should cost its own detail rather than the whole investigation's response.
    /// </remarks>
    internal static JsonElement? Parse(string? stored)
    {
        if (string.IsNullOrWhiteSpace(stored))
        {
            return null;
        }

        try
        {
            return JsonDocument.Parse(stored).RootElement.Clone();
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
