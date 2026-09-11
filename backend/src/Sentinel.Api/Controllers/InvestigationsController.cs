using System.Text.Json;
using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Common;
using Sentinel.Application.Features.Investigations;

namespace Sentinel.Api.Controllers;

/// <summary>Reading investigations. Starting one lives on the incident it belongs to.</summary>
[ApiController]
[Route("api/investigations")]
[Authorize]
public sealed class InvestigationsController : ControllerBase
{
    private readonly ISender _sender;

    public InvestigationsController(ISender sender) => _sender = sender;

    [HttpGet]
    [ProducesResponseType<IReadOnlyList<InvestigationDto>>(StatusCodes.Status200OK)]
    public async Task<ActionResult<IReadOnlyList<InvestigationDto>>> List(
        [FromQuery] Guid? incidentId,
        [FromQuery] int limit = 50,
        CancellationToken cancellationToken = default) =>
        Ok(await _sender.Send(new ListInvestigationsQuery(incidentId, limit), cancellationToken));

    /// <summary>
    /// One investigation with everything it produced: timeline, evidence, hypotheses, root cause,
    /// recommendations and the tool calls behind them.
    /// </summary>
    /// <remarks>
    /// One request rather than six. This is what the investigation screen loads, and the parts
    /// are only meaningful together — a piece of evidence is an assertion until you can see
    /// which step found it and which conclusion cited it.
    /// </remarks>
    [HttpGet("{id:guid}")]
    [ProducesResponseType<InvestigationDetailDto>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<InvestigationDetailDto>> Get(
        Guid id,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new GetInvestigationQuery(id), cancellationToken));
}

/// <summary>
/// The callback surface: one event per state transition, posted by the AI service.
/// </summary>
/// <remarks>
/// Guarded by the per-investigation callback token rather than by the internal API key. The AI
/// service holds both, and they are for different things: the key says "this is the AI service"
/// and the token says "this is the run you started", so a token that escaped one investigation
/// cannot write events into another.
/// </remarks>
[ApiController]
[Route("internal/investigations")]
[AllowAnonymous]
[ApiExplorerSettings(GroupName = "internal")]
public sealed class InvestigationCallbacksController : ControllerBase
{
    private readonly ISender _sender;
    private readonly ICallbackTokenService _tokens;

    public InvestigationCallbacksController(ISender sender, ICallbackTokenService tokens)
    {
        _sender = sender;
        _tokens = tokens;
    }

    /// <summary>Records one event of a running investigation.</summary>
    /// <remarks>
    /// Answers 200 for an event it has already seen rather than 409: delivery is at-least-once
    /// (ADR-0006), a redelivery means the AI service never saw the first answer, and a 409 would
    /// make it retry something that is already stored.
    /// </remarks>
    [HttpPost("{id:guid}/events")]
    [ProducesResponseType<InvestigationEventAck>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status401Unauthorized)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<InvestigationEventAck>> Record(
        Guid id,
        InvestigationEventBody body,
        CancellationToken cancellationToken)
    {
        if (!_tokens.Verify(id, Request.Headers[CallbackTokenHeader].ToString()))
        {
            return Unauthorized(new { message = $"A valid {CallbackTokenHeader} header is required." });
        }

        if (body.InvestigationId is { } claimed && claimed != id)
        {
            // The path is authenticated and the body is not, so they have to agree. Taking the
            // body's id would let a token for one investigation write into another.
            return BadRequest(new { message = "investigation_id does not match the URL." });
        }

        var ack = await _sender.Send(
            new RecordInvestigationEventCommand(
                id,
                body.Sequence,
                body.Type,
                body.State,
                body.Message ?? string.Empty,
                body.Payload,
                body.ToolCalls,
                body.LlmUsage,
                body.Timestamp),
            cancellationToken);

        return Ok(ack);
    }

    /// <summary>Matches <c>CALLBACK_TOKEN_HEADER</c> in the AI service's <c>agents/events.py</c>.</summary>
    public const string CallbackTokenHeader = "X-Callback-Token";
}

/// <summary>The wire shape of docs/planning.md §3.2.</summary>
public sealed record InvestigationEventBody(
    int Sequence,
    InvestigationEventType Type,
    string State,
    string? Message,
    JsonElement? Payload,
    IReadOnlyList<ToolCallRecord>? ToolCalls,
    LlmUsageRecord? LlmUsage,
    DateTimeOffset? Timestamp,
    Guid? InvestigationId = null);
