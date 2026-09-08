using System.Text.Json;
using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.ModelPredictions;

public sealed record ModelPredictionDto(
    Guid Id,
    Guid? InvestigationId,
    string ModelName,
    ModelPurpose Purpose,
    int PromptTokens,
    int CompletionTokens,
    int LatencyMs,
    bool ValidJson,
    DateTimeOffset CalledAt);

// ----------------------------------------------------------------------- commands ----

/// <summary>
/// Records one call to a language model.
/// </summary>
/// <remarks>
/// Written here rather than by the AI service directly. The AI service owns the <c>rag</c>
/// schema and the backend owns <c>sentinel</c>, and neither writes to the other's — so the
/// service that makes the call reports it, and the service that owns the table stores it.
/// </remarks>
public sealed record RecordModelPredictionCommand(
    Guid? InvestigationId,
    string ModelName,
    ModelPurpose Purpose,
    int PromptTokens,
    int CompletionTokens,
    int LatencyMs,
    bool ValidJson,
    string? Output) : IRequest<ModelPredictionDto>;

public sealed class RecordModelPredictionValidator
    : AbstractValidator<RecordModelPredictionCommand>
{
    public RecordModelPredictionValidator()
    {
        RuleFor(c => c.ModelName).NotEmpty().MaximumLength(100);
        RuleFor(c => c.PromptTokens).GreaterThanOrEqualTo(0);
        RuleFor(c => c.CompletionTokens).GreaterThanOrEqualTo(0);
        RuleFor(c => c.LatencyMs).GreaterThanOrEqualTo(0);
    }
}

public sealed class RecordModelPredictionHandler
    : IRequestHandler<RecordModelPredictionCommand, ModelPredictionDto>
{
    private readonly ISentinelDbContext _db;

    public RecordModelPredictionHandler(ISentinelDbContext db) => _db = db;

    public async Task<ModelPredictionDto> Handle(
        RecordModelPredictionCommand request,
        CancellationToken cancellationToken)
    {
        var prediction = new ModelPrediction
        {
            InvestigationId = request.InvestigationId,
            ModelName = request.ModelName,
            Purpose = request.Purpose,
            PromptTokens = request.PromptTokens,
            CompletionTokens = request.CompletionTokens,
            LatencyMs = request.LatencyMs,
            ValidJson = request.ValidJson,
            Output = AsJson(request.Output),
        };

        _db.ModelPredictions.Add(prediction);
        await _db.SaveChangesAsync(cancellationToken);

        return Map(prediction);
    }

    /// <summary>
    /// Makes any output storable in the <c>jsonb</c> column.
    /// </summary>
    /// <remarks>
    /// The interesting rows are exactly the ones whose output is not JSON: a call is recorded
    /// with <c>valid_json = false</c> precisely because the model returned something that would
    /// not parse, and that raw text is the evidence. Passed through unchanged it is rejected by
    /// PostgreSQL, the insert fails, and the row the structured-output success rate is measured
    /// from is the one row that can never be written.
    ///
    /// Text that is not JSON is stored as a JSON string, which is valid JSON, keeps the column
    /// queryable, and loses nothing.
    /// </remarks>
    private static string? AsJson(string? output)
    {
        if (string.IsNullOrWhiteSpace(output))
        {
            return null;
        }

        try
        {
            using var _ = JsonDocument.Parse(output);
            return output;
        }
        catch (JsonException)
        {
            return JsonSerializer.Serialize(output);
        }
    }

    private static ModelPredictionDto Map(ModelPrediction p) => new(
        p.Id,
        p.InvestigationId,
        p.ModelName,
        p.Purpose,
        p.PromptTokens,
        p.CompletionTokens,
        p.LatencyMs,
        p.ValidJson,
        p.CalledAt);
}

// ------------------------------------------------------------------------ queries ----

/// <summary>
/// The most recent model calls, newest first.
/// </summary>
/// <remarks>
/// Deliberately omits the stored output: it can be large, and this feeds a list view and the
/// Phase 11 evaluation dashboard, both of which want latency and token counts rather than
/// generated text.
/// </remarks>
public sealed record ListModelPredictionsQuery(Guid? InvestigationId, int Limit)
    : IRequest<IReadOnlyList<ModelPredictionDto>>;

public sealed class ListModelPredictionsHandler
    : IRequestHandler<ListModelPredictionsQuery, IReadOnlyList<ModelPredictionDto>>
{
    private readonly ISentinelDbContext _db;

    public ListModelPredictionsHandler(ISentinelDbContext db) => _db = db;

    public async Task<IReadOnlyList<ModelPredictionDto>> Handle(
        ListModelPredictionsQuery request,
        CancellationToken cancellationToken)
    {
        var query = _db.ModelPredictions.AsNoTracking();

        if (request.InvestigationId is { } id)
        {
            query = query.Where(p => p.InvestigationId == id);
        }

        return await query
            .OrderByDescending(p => p.CalledAt)
            .Take(Math.Clamp(request.Limit, 1, 200))
            .Select(p => new ModelPredictionDto(
                p.Id,
                p.InvestigationId,
                p.ModelName,
                p.Purpose,
                p.PromptTokens,
                p.CompletionTokens,
                p.LatencyMs,
                p.ValidJson,
                p.CalledAt))
            .ToListAsync(cancellationToken);
    }
}
