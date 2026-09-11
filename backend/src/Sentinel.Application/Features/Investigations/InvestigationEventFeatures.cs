using System.Text.Json;
using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.Investigations;

/// <summary>The <c>type</c> field of the callback contract in docs/planning.md §3.2.</summary>
/// <remarks>
/// Mirrors <c>EventType</c> in the AI service's <c>agents/state_machine.py</c>. The wire form is
/// snake_case; the API's JSON options convert it.
/// </remarks>
public enum InvestigationEventType
{
    StepCompleted,
    EvidenceFound,
    Hypothesis,
    RootCause,
    Recommendation,
    Completed,
    Failed,
}

/// <summary>One MCP call the agent made, as the <c>tool_calls</c> table wants it.</summary>
public sealed record ToolCallRecord(
    string Server,
    string Tool,
    JsonElement? Args,
    string? ResultSummary,
    bool Success,
    int LatencyMs);

/// <summary>What one model call cost, as <c>model_predictions</c> wants it.</summary>
public sealed record LlmUsageRecord(
    string? Model,
    int PromptTokens,
    int CompletionTokens,
    int LatencyMs,
    int Retries);

/// <summary>
/// One callback event from the AI service.
/// </summary>
/// <remarks>
/// <see cref="Sequence"/> is assigned by the agent when the event is emitted rather than when it
/// is delivered, so a gap means an event was lost rather than reordered (ADR-0006). It is unique
/// per investigation, which is also how a redelivered event is recognised: delivery is
/// at-least-once.
/// </remarks>
public sealed record RecordInvestigationEventCommand(
    Guid InvestigationId,
    int Sequence,
    InvestigationEventType Type,
    string State,
    string Message,
    JsonElement? Payload,
    IReadOnlyList<ToolCallRecord>? ToolCalls,
    LlmUsageRecord? LlmUsage,
    DateTimeOffset? Timestamp) : IRequest<InvestigationEventAck>;

/// <param name="Applied">False when the event had already been recorded and was ignored.</param>
public sealed record InvestigationEventAck(Guid InvestigationId, int Sequence, bool Applied);

public sealed class RecordInvestigationEventValidator : AbstractValidator<RecordInvestigationEventCommand>
{
    public RecordInvestigationEventValidator()
    {
        RuleFor(c => c.InvestigationId).NotEmpty();
        RuleFor(c => c.Sequence).GreaterThan(0);
        RuleFor(c => c.Type).IsInEnum();
        RuleFor(c => c.State).NotEmpty().MaximumLength(64);

        // Not MaximumLength: the agent writes a sentence a model produced, and refusing the
        // event would throw away a step over its prose. The handler truncates instead.
        RuleFor(c => c.Message).NotNull();
    }
}

/// <summary>
/// Writes one callback event into the rows it describes, and tells the browsers watching.
/// </summary>
/// <remarks>
/// <para>
/// Two things arrive here, and the difference is the whole design. A step event is a fact about
/// the timeline and is written as it comes. A terminal event carries the entire investigation —
/// evidence, hypotheses, root cause, recommendations — because ADR-0002 accepts that an
/// intermediate event can be lost, and only accepts it because the last one can rebuild what the
/// lost ones would have written. So the terminal event does not merely close the row: it
/// reconciles, inserting whatever is missing and filling in what arrived incomplete.
/// </para>
/// <para>
/// That makes every write here idempotent by construction. Evidence is matched by its summary,
/// hypotheses and recommendations by their text, the root cause by there being one — so a
/// redelivered event updates a row instead of duplicating it, and reconciliation is the same
/// code path as the first write rather than a second one that can disagree with it.
/// </para>
/// </remarks>
public sealed class RecordInvestigationEventHandler
    : IRequestHandler<RecordInvestigationEventCommand, InvestigationEventAck>
{
    private readonly ISentinelDbContext _db;
    private readonly IInvestigationNotifier _notifier;
    private readonly ILogger<RecordInvestigationEventHandler> _logger;

    public RecordInvestigationEventHandler(
        ISentinelDbContext db,
        IInvestigationNotifier notifier,
        ILogger<RecordInvestigationEventHandler> logger)
    {
        _db = db;
        _notifier = notifier;
        _logger = logger;
    }

    public async Task<InvestigationEventAck> Handle(
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var investigation = await _db.Investigations
            .Include(i => i.Incident)
            .Include(i => i.Evidence)
            .Include(i => i.Hypotheses)
            .Include(i => i.RootCauses)
                .ThenInclude(r => r.Recommendations)
            .FirstOrDefaultAsync(i => i.Id == request.InvestigationId, cancellationToken)
            ?? throw new NotFoundException(nameof(Investigation), request.InvestigationId);

        var applied = request.Type switch
        {
            InvestigationEventType.StepCompleted => await StepAsync(investigation, request, cancellationToken),
            InvestigationEventType.EvidenceFound => await EvidenceAsync(investigation, request, cancellationToken),
            InvestigationEventType.Hypothesis => await HypothesisAsync(investigation, request, cancellationToken),
            InvestigationEventType.RootCause => await RootCauseAsync(investigation, request, cancellationToken),
            InvestigationEventType.Recommendation => RecommendationEvent(investigation, request),
            _ => await FinishAsync(investigation, request, cancellationToken),
        };

        await _db.SaveChangesAsync(cancellationToken);

        return new InvestigationEventAck(investigation.Id, request.Sequence, applied);
    }

    // ------------------------------------------------------------------- the timeline ----

    private async Task<bool> StepAsync(
        Investigation investigation,
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var duplicate = await _db.InvestigationSteps.AnyAsync(
            s => s.InvestigationId == investigation.Id && s.Sequence == request.Sequence,
            cancellationToken);

        if (duplicate)
        {
            // At-least-once delivery means this is expected rather than alarming: the AI service
            // retries an event whose response it never saw.
            _logger.LogDebug(
                "Investigation {InvestigationId} step {Sequence} was already recorded",
                investigation.Id,
                request.Sequence);

            return false;
        }

        var completedAt = request.Timestamp ?? DateTimeOffset.UtcNow;
        var durationMs = request.Payload.Int("duration_ms");

        var step = new InvestigationStep
        {
            InvestigationId = investigation.Id,
            Sequence = request.Sequence,
            State = Text.Truncate(request.State, 64)!,
            Message = Text.Truncate(request.Message, 2000) ?? request.State,
            Payload = request.Payload.AsJson(),
            DurationMs = durationMs,
            // Derived rather than sent: the agent times a step and the event says when it ended,
            // so the start is the one thing that would be a second clock's opinion.
            StartedAt = durationMs is { } ms ? completedAt.AddMilliseconds(-ms) : completedAt,
            CompletedAt = completedAt,
        };

        _db.InvestigationSteps.Add(step);

        foreach (var call in request.ToolCalls ?? [])
        {
            _db.ToolCalls.Add(new ToolCall
            {
                InvestigationId = investigation.Id,
                StepId = step.Id,
                Server = Text.Truncate(call.Server, 64)!,
                Tool = Text.Truncate(call.Tool, 100)!,
                Args = call.Args.AsJson(),
                ResultSummary = Text.Truncate(call.ResultSummary, 2000),
                Success = call.Success,
                LatencyMs = call.LatencyMs,

                // Phase 7's agent has read-only tools only, and the AI service's policy layer is
                // what enforces that. Recording it as false here would be this service asserting
                // something it cannot see; it is the default, and Phase 10 sets it from the call.
                CalledAt = completedAt,
            });
        }

        if (request.LlmUsage is { } usage)
        {
            // The agent's model calls land in model_predictions without a second round trip:
            // the event already carries what the row needs, and a separate POST per call would
            // double the traffic of the thing being measured.
            _db.ModelPredictions.Add(new ModelPrediction
            {
                InvestigationId = investigation.Id,
                ModelName = Text.Truncate(usage.Model, 100) ?? "unknown",
                Purpose = request.State == "VALIDATE" ? ModelPurpose.Validation : ModelPurpose.Reasoning,
                PromptTokens = usage.PromptTokens,
                CompletionTokens = usage.CompletionTokens,
                LatencyMs = usage.LatencyMs,

                // The event exists, so the call produced something the agent could use. A call
                // that never validated ends the run instead, and is reported by the step that
                // stopped it.
                ValidJson = true,
                CalledAt = completedAt,
            });
        }

        await _notifier.StepUpdatedAsync(
            investigation.Id,
            new InvestigationStepDto(
                step.Id,
                step.Sequence,
                step.State,
                step.Message,
                Json.Parse(step.Payload),
                step.DurationMs,
                step.StartedAt,
                step.CompletedAt),
            cancellationToken);

        return true;
    }

    // ------------------------------------------------------------------- the findings ----

    private async Task<bool> EvidenceAsync(
        Investigation investigation,
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var summary = Text.Truncate(request.Message, 2000) ?? request.State;

        if (investigation.Evidence.Any(e => e.Summary == summary))
        {
            return false;
        }

        // The step that found it. The runner emits a state's own step event before the events
        // the node produced, so the step this belongs to is already there.
        var stepId = await _db.InvestigationSteps
            .Where(s => s.InvestigationId == investigation.Id && s.State == request.State)
            .OrderByDescending(s => s.Sequence)
            .Select(s => (Guid?)s.Id)
            .FirstOrDefaultAsync(cancellationToken);

        var evidence = new Evidence
        {
            InvestigationId = investigation.Id,
            StepId = stepId,
            Source = Sources.Parse(request.Payload.String("source")),
            Summary = summary,
            Weight = request.Payload.Decimal("weight") ?? 0.5m,
            Raw = request.Payload.Object("raw"),
        };

        investigation.Evidence.Add(evidence);
        _db.Evidence.Add(evidence);

        await _notifier.EvidenceAddedAsync(
            investigation.Id,
            new EvidenceDto(
                evidence.Id,
                evidence.StepId,
                evidence.Source,
                evidence.Summary,
                evidence.Weight,
                Json.Parse(evidence.Raw),
                evidence.CreatedAt),
            cancellationToken);

        return true;
    }

    private async Task<bool> HypothesisAsync(
        Investigation investigation,
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var title = Text.Truncate(request.Payload.String("title") ?? request.Message, 300)!;
        var hypothesis = Upsert(
            investigation,
            title,
            request.Payload.String("description"),
            request.Payload.Decimal("score") ?? 0m,
            request.Payload.Int("rank") ?? 0,
            request.Payload.Bool("selected") ?? false);

        await _notifier.HypothesisUpdatedAsync(
            investigation.Id,
            new HypothesisDto(
                hypothesis.Id,
                hypothesis.Title,
                hypothesis.Description,
                hypothesis.Score,
                hypothesis.Rank,
                hypothesis.IsSelected),
            cancellationToken);

        return true;
    }

    private async Task<bool> RootCauseAsync(
        Investigation investigation,
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var rootCause = UpsertRootCause(investigation, request.Payload);

        await _notifier.RootCauseFoundAsync(
            investigation.Id,
            new RootCauseDto(
                rootCause.Id,
                rootCause.HypothesisId,
                rootCause.Title,
                rootCause.Category,
                rootCause.Confidence,
                rootCause.Explanation,
                Json.Parse(rootCause.ValidatorOutput),
                rootCause.ValidatorConfidence),
            cancellationToken);

        return true;
    }

    private bool RecommendationEvent(Investigation investigation, RecordInvestigationEventCommand request)
    {
        var rootCause = Selected(investigation);

        if (rootCause is null)
        {
            // RECOMMEND_FIX only runs after VALIDATE accepted a conclusion, so this means the
            // root cause event was lost. The terminal event will bring both.
            _logger.LogWarning(
                "Investigation {InvestigationId} recommended a fix before a root cause arrived",
                investigation.Id);

            return false;
        }

        return UpsertRecommendation(investigation, rootCause, request.Payload) is not null;
    }

    // ------------------------------------------------------------------- the end ----

    private async Task<bool> FinishAsync(
        Investigation investigation,
        RecordInvestigationEventCommand request,
        CancellationToken cancellationToken)
    {
        var result = request.Payload.Element("result");

        if (result is not null)
        {
            Reconcile(investigation, result);
        }

        investigation.Status = request.Type == InvestigationEventType.Failed
            ? InvestigationStatus.Failed
            : InvestigationStatus.Completed;

        investigation.CompletedAt = request.Timestamp ?? DateTimeOffset.UtcNow;
        investigation.TotalDurationMs = request.Payload.Int("duration_ms")
            ?? result.Int("duration_ms")
            ?? investigation.TotalDurationMs;

        // Present on a completed run too: an investigation that stopped for a human has a reason
        // worth reading, and it is the same sentence the timeline ends with.
        investigation.FailureReason = Text.Truncate(result.String("failure_reason") ?? FailureFrom(request), 1000);
        investigation.RouterIntent = Text.Truncate(result.String("router_intent"), 64);
        investigation.RouterOutput = result.Object("router_output");

        var usage = result.Element("usage");

        if (usage is not null)
        {
            investigation.LlmCalls = usage.Int("llm_calls") ?? investigation.LlmCalls;
            investigation.ToolCalls = usage.Int("tool_calls") ?? investigation.ToolCalls;
            investigation.PromptTokens = usage.Int("prompt_tokens") ?? investigation.PromptTokens;
            investigation.CompletionTokens = usage.Int("completion_tokens") ?? investigation.CompletionTokens;
        }

        UpdateIncident(investigation);

        var rootCause = Selected(investigation);

        await _notifier.InvestigationCompletedAsync(
            investigation.Id,
            StartInvestigationHandler.Map(
                investigation,
                investigation.Incident?.IncidentCode ?? string.Empty,
                rootCause,
                await _db.InvestigationSteps.CountAsync(s => s.InvestigationId == investigation.Id, cancellationToken),
                investigation.Evidence.Count),
            cancellationToken);

        return true;
    }

    /// <summary>
    /// Writes whatever the streamed events did not.
    /// </summary>
    /// <remarks>
    /// The reason the final payload is worth its size. Every collection is matched against what
    /// is already stored, so a run whose events all arrived changes nothing here and a run that
    /// lost half of them ends with the same rows either way — which is the promise ADR-0002 made
    /// when it accepted that delivery is not durable.
    /// </remarks>
    private void Reconcile(Investigation investigation, JsonElement? result)
    {
        foreach (var item in result.Array("evidence"))
        {
            var summary = Text.Truncate(item.String("summary"), 2000);

            if (summary is null)
            {
                continue;
            }

            var existing = investigation.Evidence.FirstOrDefault(e => e.Summary == summary);

            if (existing is null)
            {
                var evidence = new Evidence
                {
                    InvestigationId = investigation.Id,
                    Source = Sources.Parse(item.String("source")),
                    Summary = summary,
                    Weight = item.Decimal("weight") ?? 0.5m,
                    Raw = item.Object("raw"),
                };

                investigation.Evidence.Add(evidence);
                _db.Evidence.Add(evidence);
            }
            else
            {
                // The streamed evidence event carries the summary but not the tool output behind
                // it, because a timeline does not want it and this is where it belongs.
                existing.Raw ??= item.Object("raw");
            }
        }

        foreach (var item in result.Array("hypotheses"))
        {
            var title = Text.Truncate(item.String("title"), 300);

            if (title is not null)
            {
                Upsert(
                    investigation,
                    title,
                    item.String("description"),
                    item.Decimal("score") ?? 0m,
                    item.Int("rank") ?? 0,
                    item.Bool("selected") ?? false);
            }
        }

        if (result.Element("root_cause") is { ValueKind: JsonValueKind.Object } rootCausePayload)
        {
            var rootCause = UpsertRootCause(investigation, rootCausePayload);

            foreach (var item in result.Array("recommendations"))
            {
                UpsertRecommendation(investigation, rootCause, item);
            }
        }
    }

    /// <summary>
    /// Moves the incident on, or back.
    /// </summary>
    /// <remarks>
    /// A conclusion with actions to take is what <see cref="IncidentStatus.AwaitingApproval"/>
    /// means, and it is the state the approval screen lists. Anything else returns the incident
    /// to <see cref="IncidentStatus.Open"/> — including a run that concluded and would not stand
    /// behind it, because nothing is investigating it any more and leaving it in
    /// <c>Investigating</c> would hide it behind a status that implies work in progress.
    /// </remarks>
    private static void UpdateIncident(Investigation investigation)
    {
        if (investigation.Incident is not { } incident)
        {
            return;
        }

        var actionable = investigation.Status == InvestigationStatus.Completed
            && Selected(investigation)?.Recommendations.Count > 0;

        incident.Status = actionable ? IncidentStatus.AwaitingApproval : IncidentStatus.Open;
    }

    // ------------------------------------------------------------------- upserts ----

    private Hypothesis Upsert(
        Investigation investigation,
        string title,
        string? description,
        decimal score,
        int rank,
        bool selected)
    {
        var hypothesis = investigation.Hypotheses.FirstOrDefault(h => h.Title == title);

        if (hypothesis is null)
        {
            hypothesis = new Hypothesis { InvestigationId = investigation.Id, Title = title };
            investigation.Hypotheses.Add(hypothesis);
            _db.Hypotheses.Add(hypothesis);
        }

        // Ranking runs again after the critic rejects a conclusion, so a hypothesis can be
        // rescored during one investigation and the later score is the one that counts.
        hypothesis.Description = Text.Truncate(description, 4000) ?? hypothesis.Description;
        hypothesis.Score = score;
        hypothesis.Rank = rank;
        hypothesis.IsSelected = selected || hypothesis.IsSelected;

        return hypothesis;
    }

    private RootCause UpsertRootCause(Investigation investigation, JsonElement? payload)
    {
        var rootCause = Selected(investigation);

        if (rootCause is null)
        {
            rootCause = new RootCause
            {
                InvestigationId = investigation.Id,
                Title = string.Empty,
                Category = Categories.Unknown,
            };

            investigation.RootCauses.Add(rootCause);
            _db.RootCauses.Add(rootCause);
        }

        rootCause.Title = Text.Truncate(payload.String("title"), 300) ?? rootCause.Title;
        rootCause.Category = Text.Truncate(payload.String("category"), 64) ?? rootCause.Category;
        rootCause.Confidence = payload.Decimal("confidence") ?? rootCause.Confidence;
        rootCause.Explanation = Text.Truncate(payload.String("explanation"), 8000) ?? rootCause.Explanation;
        rootCause.ValidatorOutput = payload.Object("validator_output") ?? rootCause.ValidatorOutput;
        rootCause.ValidatorConfidence = payload.Decimal("validator_confidence") ?? rootCause.ValidatorConfidence;

        // The agent names the hypothesis it chose rather than its id, because the id is this
        // database's and the agent has never seen it.
        if (rootCause.HypothesisId is null && payload.String("hypothesis") is { } chosen)
        {
            rootCause.HypothesisId = investigation.Hypotheses.FirstOrDefault(h => h.Title == chosen)?.Id;
        }

        return rootCause;
    }

    private Recommendation? UpsertRecommendation(
        Investigation investigation,
        RootCause rootCause,
        JsonElement? payload)
    {
        var actionCode = Text.Truncate(payload.String("action_code"), 64);

        if (actionCode is null)
        {
            return null;
        }

        var recommendation = rootCause.Recommendations.FirstOrDefault(r => r.ActionCode == actionCode);

        if (recommendation is null)
        {
            recommendation = new Recommendation
            {
                InvestigationId = investigation.Id,
                RootCauseId = rootCause.Id,
                ActionCode = actionCode,
                Description = string.Empty,
            };

            rootCause.Recommendations.Add(recommendation);
            _db.Recommendations.Add(recommendation);
        }

        recommendation.Description = Text.Truncate(payload.String("description"), 2000) ?? recommendation.Description;
        recommendation.ToolName = Text.Truncate(payload.String("tool_name"), 100) ?? recommendation.ToolName;
        recommendation.ToolArgs = payload.Object("tool_args") ?? recommendation.ToolArgs;

        // Never taken from the payload. Phase 10 executes these, and whether a human has to
        // approve one is this system's rule rather than something the agent gets to decide.
        recommendation.RequiresApproval = true;

        return recommendation;
    }

    private static RootCause? Selected(Investigation investigation) =>
        investigation.RootCauses.OrderByDescending(r => r.Confidence).FirstOrDefault();

    private static string? FailureFrom(RecordInvestigationEventCommand request) =>
        request.Type == InvestigationEventType.Failed ? request.Message : null;
}

/// <summary>The category written when the agent would not name one. Mirrors <c>agents/categories.py</c>.</summary>
internal static class Categories
{
    internal const string Unknown = "UNKNOWN";
}

internal static class Text
{
    /// <summary>
    /// Cuts a value to what its column holds.
    /// </summary>
    /// <remarks>
    /// Every long string here was written by a language model against a length it does not know
    /// about. Refusing the event would cost a step of the timeline over prose; truncating costs
    /// the tail of one sentence, and the full text is in the step payload either way.
    /// </remarks>
    internal static string? Truncate(string? value, int max) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Length <= max ? value : value[..max];
}

internal static class Sources
{
    /// <summary>
    /// The wire's <c>historical_incident</c> as the enum's <c>HistoricalIncident</c>.
    /// </summary>
    /// <remarks>
    /// Unknown values become <see cref="EvidenceSource.RagDocument"/> rather than throwing. A
    /// source this backend has not heard of is a newer AI service, and losing the fact would be
    /// a worse answer than filing it under the least specific kind.
    /// </remarks>
    internal static EvidenceSource Parse(string? value) =>
        Enum.TryParse<EvidenceSource>(value?.Replace("_", string.Empty), ignoreCase: true, out var source)
            ? source
            : EvidenceSource.RagDocument;
}

/// <summary>Reading values out of a payload the AI service wrote, without trusting its shape.</summary>
/// <remarks>
/// Every accessor returns null rather than throwing on a missing or mistyped field. These
/// documents come from another service and partly from a model; a callback that lost a whole
/// step because one number arrived as a string would be a brittle contract, and the payload is
/// stored verbatim regardless, so nothing is lost that cannot be read back.
/// </remarks>
internal static class PayloadExtensions
{
    internal static JsonElement? Element(this JsonElement? payload, string name) =>
        payload is { ValueKind: JsonValueKind.Object } element
            && element.TryGetProperty(name, out var value)
            && value.ValueKind is not JsonValueKind.Null
            ? value
            : null;

    internal static string? String(this JsonElement? payload, string name) =>
        payload.Element(name) is { ValueKind: JsonValueKind.String } value ? value.GetString() : null;

    internal static int? Int(this JsonElement? payload, string name) =>
        payload.Element(name) is { ValueKind: JsonValueKind.Number } value && value.TryGetInt32(out var number)
            ? number
            : null;

    internal static decimal? Decimal(this JsonElement? payload, string name) =>
        payload.Element(name) is { ValueKind: JsonValueKind.Number } value && value.TryGetDecimal(out var number)
            ? number
            : null;

    internal static bool? Bool(this JsonElement? payload, string name) =>
        payload.Element(name) is { ValueKind: JsonValueKind.True or JsonValueKind.False } value
            ? value.GetBoolean()
            : null;

    /// <summary>A nested document as the text a jsonb column stores.</summary>
    internal static string? Object(this JsonElement? payload, string name) =>
        payload.Element(name) is { ValueKind: JsonValueKind.Object or JsonValueKind.Array } value
            ? value.GetRawText()
            : null;

    internal static IEnumerable<JsonElement?> Array(this JsonElement? payload, string name)
    {
        if (payload.Element(name) is not { ValueKind: JsonValueKind.Array } value)
        {
            yield break;
        }

        foreach (var item in value.EnumerateArray())
        {
            yield return item;
        }
    }

    /// <summary>The whole payload as the text a jsonb column stores.</summary>
    internal static string? AsJson(this JsonElement? payload) =>
        payload is { ValueKind: not JsonValueKind.Null and not JsonValueKind.Undefined } element
            ? element.GetRawText()
            : null;
}
