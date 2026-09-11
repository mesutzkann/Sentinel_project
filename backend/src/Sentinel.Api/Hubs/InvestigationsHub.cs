using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.SignalR;
using Sentinel.Application.Features.Investigations;

namespace Sentinel.Api.Hubs;

/// <summary>
/// The live half of an investigation: every step, as it happens.
/// </summary>
/// <remarks>
/// <para>
/// A run is tens of seconds to minutes of model and tool calls, and a timeline filling in while
/// it happens is the most visible thing this project does. That is the whole reason ADR-0002
/// chose callbacks over a synchronous call — this hub is the other end of that decision.
/// </para>
/// <para>
/// Clients subscribe to one investigation rather than receiving everything. A browser showing
/// one incident has no use for another's steps, and without groups every open tab would receive
/// every event of every run in progress.
/// </para>
/// </remarks>
[Authorize]
public sealed class InvestigationsHub : Hub
{
    public const string Path = "/hubs/investigations";

    /// <summary>Client method names, so the hub and its notifier cannot drift apart.</summary>
    public static class Events
    {
        public const string StepUpdated = "StepUpdated";
        public const string EvidenceAdded = "EvidenceAdded";
        public const string HypothesisUpdated = "HypothesisUpdated";
        public const string RootCauseFound = "RootCauseFound";
        public const string InvestigationCompleted = "InvestigationCompleted";
    }

    /// <summary>Start receiving one investigation's events.</summary>
    /// <remarks>
    /// The group name is the investigation id, so a client that reconnects rejoins the same
    /// group and a client watching two investigations joins two.
    /// </remarks>
    public Task Subscribe(Guid investigationId) =>
        Groups.AddToGroupAsync(Context.ConnectionId, Group(investigationId));

    public Task Unsubscribe(Guid investigationId) =>
        Groups.RemoveFromGroupAsync(Context.ConnectionId, Group(investigationId));

    internal static string Group(Guid investigationId) => $"investigation:{investigationId}";
}

/// <summary>
/// Pushes what the callback handler wrote to the browsers watching that investigation.
/// </summary>
/// <remarks>
/// Every push is wrapped: a closed WebSocket, a client that vanished mid-send, a hub that is
/// shutting down. None of those may turn into a failed callback, because the AI service would
/// then retry an event the database has already stored — and the browser can always reload and
/// read the same rows over HTTP. The notification is the convenience; the row is the record.
/// </remarks>
public sealed class SignalRInvestigationNotifier : IInvestigationNotifier
{
    private readonly IHubContext<InvestigationsHub> _hub;
    private readonly ILogger<SignalRInvestigationNotifier> _logger;

    public SignalRInvestigationNotifier(
        IHubContext<InvestigationsHub> hub,
        ILogger<SignalRInvestigationNotifier> logger)
    {
        _hub = hub;
        _logger = logger;
    }

    public Task StepUpdatedAsync(Guid investigationId, InvestigationStepDto step, CancellationToken cancellationToken = default) =>
        Send(investigationId, InvestigationsHub.Events.StepUpdated, step, cancellationToken);

    public Task EvidenceAddedAsync(Guid investigationId, EvidenceDto evidence, CancellationToken cancellationToken = default) =>
        Send(investigationId, InvestigationsHub.Events.EvidenceAdded, evidence, cancellationToken);

    public Task HypothesisUpdatedAsync(Guid investigationId, HypothesisDto hypothesis, CancellationToken cancellationToken = default) =>
        Send(investigationId, InvestigationsHub.Events.HypothesisUpdated, hypothesis, cancellationToken);

    public Task RootCauseFoundAsync(Guid investigationId, RootCauseDto rootCause, CancellationToken cancellationToken = default) =>
        Send(investigationId, InvestigationsHub.Events.RootCauseFound, rootCause, cancellationToken);

    public Task InvestigationCompletedAsync(Guid investigationId, InvestigationDto investigation, CancellationToken cancellationToken = default) =>
        Send(investigationId, InvestigationsHub.Events.InvestigationCompleted, investigation, cancellationToken);

    private async Task Send(Guid investigationId, string method, object payload, CancellationToken cancellationToken)
    {
        try
        {
            await _hub.Clients
                .Group(InvestigationsHub.Group(investigationId))
                .SendAsync(method, investigationId, payload, cancellationToken);
        }
        catch (Exception exception)
        {
            _logger.LogWarning(
                exception,
                "Could not push {Method} for investigation {InvestigationId}",
                method,
                investigationId);
        }
    }
}
