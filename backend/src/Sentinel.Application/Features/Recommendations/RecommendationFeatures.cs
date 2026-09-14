using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.Recommendations;

/// <summary>
/// Approving a fix, running it, and recording what it did.
/// </summary>
/// <remarks>
/// <para>
/// This is the human in the loop. The agent proposes; nothing it proposed touches a running
/// service until somebody here says so, and what they said is recorded against the row: who,
/// when, and what the measurement showed afterwards.
/// </para>
/// <para>
/// **The approval names the action.** The token is minted from this row's tool and arguments, so
/// it authorises that call and no other — a recommendation whose arguments are edited between
/// approval and execution fails verification at the AI service. The status moves
/// PendingApproval → Approved → Executing → Executed | Verified | Failed, and it moves to
/// Verified only when the AI service says the symptom measurably improved.
/// </para>
/// </remarks>
public sealed record ApproveRecommendationCommand(Guid RecommendationId, Guid ApprovedBy)
    : IRequest<RecommendationOutcome>;

public sealed record RejectRecommendationCommand(Guid RecommendationId, Guid RejectedBy, string? Reason)
    : IRequest<RecommendationOutcome>;

/// <summary>What became of a recommendation, for the screen that asked.</summary>
public sealed record RecommendationOutcome(
    Guid Id,
    RecommendationStatus Status,
    bool Executed,
    bool Confirmed,
    string Verdict,
    string Summary,
    string? Error);

public sealed class ApproveRecommendationHandler
    : IRequestHandler<ApproveRecommendationCommand, RecommendationOutcome>
{
    private readonly ISentinelDbContext _database;
    private readonly IApprovalTokenService _approvals;
    private readonly IAiServiceClient _ai;
    private readonly ILogger<ApproveRecommendationHandler> _logger;

    public ApproveRecommendationHandler(
        ISentinelDbContext database,
        IApprovalTokenService approvals,
        IAiServiceClient ai,
        ILogger<ApproveRecommendationHandler> logger)
    {
        _database = database;
        _approvals = approvals;
        _ai = ai;
        _logger = logger;
    }

    public async Task<RecommendationOutcome> Handle(
        ApproveRecommendationCommand request,
        CancellationToken cancellationToken)
    {
        var recommendation = await Load(request.RecommendationId, cancellationToken);

        if (recommendation.Status is not RecommendationStatus.PendingApproval)
        {
            // Not an error worth a 500 and not something to do twice. A second approval of an
            // executed action would mint a second token for a change that has already happened.
            throw new ConflictException(
                $"This recommendation is {recommendation.Status} and only a pending one can be approved.");
        }

        if (string.IsNullOrWhiteSpace(recommendation.ToolName))
        {
            // Advisory recommendations exist and are not executable: "add an alert for pool
            // wait time" is a good recommendation with nothing to run.
            throw new ValidationException(
                "This recommendation has no tool behind it, so there is nothing to execute. "
                + "Mark it done by hand.",
                []);
        }

        if (!_approvals.IsConfigured)
        {
            throw new ValidationException(
                "No approval secret is configured, so no action can be authorised. Set "
                + "Internal:ApprovalSecret to the value the AI service verifies with.",
                []);
        }

        recommendation.Status = RecommendationStatus.Approved;
        recommendation.ApprovedBy = request.ApprovedBy;
        recommendation.ApprovedAt = DateTimeOffset.UtcNow;

        // Saved before the call, deliberately. If the AI service is unreachable, the fact that a
        // person approved this is still true and still recorded — losing it would mean a second
        // approval for the same decision.
        await _database.SaveChangesAsync(cancellationToken);

        var approver = await _database.Users
            .Where(user => user.Id == request.ApprovedBy)
            .Select(user => user.Username)
            .FirstOrDefaultAsync(cancellationToken);

        var token = _approvals.Issue(
            recommendation.Id,
            recommendation.ToolName!,
            recommendation.ToolArgs,
            approver);

        recommendation.Status = RecommendationStatus.Executing;
        await _database.SaveChangesAsync(cancellationToken);

        ExecuteActionResult result;

        try
        {
            result = await _ai.ExecuteActionAsync(
                new ExecuteActionRequest(
                    recommendation.Id,
                    recommendation.ToolName!,
                    recommendation.ToolArgs,
                    token,
                    ServiceOf(recommendation),
                    recommendation.InvestigationId),
                cancellationToken);
        }
        catch (AiServiceUnavailableException exception)
        {
            // Failed, and the row says why. The alternative — leaving it Executing — is a row
            // that nobody can act on and that looks like work in progress for ever.
            recommendation.Status = RecommendationStatus.Failed;
            recommendation.ExecutionResult = $"{{\"error\":{System.Text.Json.JsonSerializer.Serialize(exception.Message)}}}";
            await _database.SaveChangesAsync(cancellationToken);

            _logger.LogWarning(
                exception, "Recommendation {RecommendationId} could not be executed", recommendation.Id);

            throw;
        }

        recommendation.ExecutedAt = DateTimeOffset.UtcNow;
        recommendation.ExecutionResult = result.RawJson;
        recommendation.VerificationResult = result.RawJson;
        recommendation.Status = Outcome(result);

        await _database.SaveChangesAsync(cancellationToken);

        _logger.LogInformation(
            "Recommendation {RecommendationId} {Status} ({Verdict}) approved by {User}",
            recommendation.Id,
            recommendation.Status,
            result.Verdict,
            approver ?? request.ApprovedBy.ToString());

        return new RecommendationOutcome(
            recommendation.Id,
            recommendation.Status,
            result.Executed,
            result.Confirmed,
            result.Verdict,
            result.Summary,
            result.Error);
    }

    /// <summary>
    /// Executed, verified, or failed — and the difference between the first two is a measurement.
    /// </summary>
    /// <remarks>
    /// <see cref="RecommendationStatus.Verified"/> means the AI service re-measured the symptom
    /// and it improved. An action that ran perfectly and changed nothing is Executed, which is
    /// the honest word for it: something was done and the incident is still there.
    /// </remarks>
    private static RecommendationStatus Outcome(ExecuteActionResult result) => result switch
    {
        { Executed: false } => RecommendationStatus.Failed,
        { Confirmed: true } => RecommendationStatus.Verified,
        _ => RecommendationStatus.Executed,
    };

    /// <summary>Which service this is about, for the verification to measure.</summary>
    private static string? ServiceOf(Recommendation recommendation) =>
        recommendation.Investigation?.Incident?.Service?.Name;

    private async Task<Recommendation> Load(Guid id, CancellationToken cancellationToken)
    {
        var recommendation = await _database.Recommendations
            .Include(item => item.Investigation)
                .ThenInclude(investigation => investigation!.Incident)
                    .ThenInclude(incident => incident!.Service)
            .FirstOrDefaultAsync(item => item.Id == id, cancellationToken);

        return recommendation ?? throw new NotFoundException("Recommendation", id);
    }
}

public sealed class RejectRecommendationHandler
    : IRequestHandler<RejectRecommendationCommand, RecommendationOutcome>
{
    private readonly ISentinelDbContext _database;

    public RejectRecommendationHandler(ISentinelDbContext database)
    {
        _database = database;
    }

    public async Task<RecommendationOutcome> Handle(
        RejectRecommendationCommand request,
        CancellationToken cancellationToken)
    {
        var recommendation = await _database.Recommendations
            .FirstOrDefaultAsync(item => item.Id == request.RecommendationId, cancellationToken)
            ?? throw new NotFoundException("Recommendation", request.RecommendationId);

        if (recommendation.Status is not RecommendationStatus.PendingApproval)
        {
            throw new ConflictException(
                $"This recommendation is {recommendation.Status} and only a pending one can be rejected.");
        }

        recommendation.Status = RecommendationStatus.Rejected;
        recommendation.ApprovedBy = request.RejectedBy;
        recommendation.ApprovedAt = DateTimeOffset.UtcNow;

        // The reason goes in the execution result rather than a column of its own: it is the
        // record of what happened to this recommendation, and "a person said no because the
        // pool size is deliberate" is exactly the thing the next investigation should read.
        recommendation.ExecutionResult = System.Text.Json.JsonSerializer.Serialize(new
        {
            rejected = true,
            reason = request.Reason,
        });

        await _database.SaveChangesAsync(cancellationToken);

        return new RecommendationOutcome(
            recommendation.Id,
            recommendation.Status,
            Executed: false,
            Confirmed: false,
            Verdict: "rejected",
            Summary: request.Reason ?? "Rejected.",
            Error: null);
    }
}
