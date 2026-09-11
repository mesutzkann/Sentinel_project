using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Features.Incidents;
using Sentinel.Application.Features.Investigations;
using Sentinel.Domain;
using Sentinel.Infrastructure;

namespace Sentinel.Api.Controllers;

[ApiController]
[Route("api/incidents")]
[Authorize]
public sealed class IncidentsController : ControllerBase
{
    private readonly ISender _sender;

    public IncidentsController(ISender sender) => _sender = sender;

    [HttpGet]
    [ProducesResponseType<IReadOnlyList<IncidentDto>>(StatusCodes.Status200OK)]
    public async Task<ActionResult<IReadOnlyList<IncidentDto>>> List(
        [FromQuery] Guid? serviceId,
        [FromQuery] IncidentStatus? status,
        [FromQuery] bool activeOnly = false,
        [FromQuery] int limit = 50,
        CancellationToken cancellationToken = default) =>
        Ok(await _sender.Send(
            new ListIncidentsQuery(serviceId, status, activeOnly, limit), cancellationToken));

    [HttpGet("{id:guid}")]
    [ProducesResponseType<IncidentDto>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<IncidentDto>> Get(Guid id, CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new GetIncidentQuery(id), cancellationToken));

    [HttpPost]
    [Authorize(Policy = Policies.RequireEngineer)]
    [ProducesResponseType<IncidentDto>(StatusCodes.Status201Created)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    public async Task<ActionResult<IncidentDto>> Create(
        CreateIncidentCommand command,
        CancellationToken cancellationToken)
    {
        var incident = await _sender.Send(command, cancellationToken);
        return CreatedAtAction(nameof(Get), new { id = incident.Id }, incident);
    }

    [HttpPatch("{id:guid}/status")]
    [Authorize(Policy = Policies.RequireEngineer)]
    [ProducesResponseType<IncidentDto>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<IncidentDto>> UpdateStatus(
        Guid id,
        UpdateIncidentStatusBody body,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new UpdateIncidentStatusCommand(id, body.Status), cancellationToken));

    /// <summary>Hands the incident to the agent.</summary>
    /// <remarks>
    /// 202, not 201: the row exists immediately and the investigation it describes has barely
    /// started. What it concluded arrives over the callback surface and the SignalR hub, and is
    /// readable at <c>GET /api/investigations/{id}</c> the whole time.
    /// </remarks>
    [HttpPost("{id:guid}/investigate")]
    [Authorize(Policy = Policies.RequireEngineer)]
    [ProducesResponseType<InvestigationDto>(StatusCodes.Status202Accepted)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    [ProducesResponseType(StatusCodes.Status409Conflict)]
    [ProducesResponseType(StatusCodes.Status502BadGateway)]
    public async Task<ActionResult<InvestigationDto>> Investigate(
        Guid id,
        InvestigateBody? body,
        CancellationToken cancellationToken)
    {
        var investigation = await _sender.Send(
            new StartInvestigationCommand(id, body?.Query, body?.ServiceHint), cancellationToken);

        return Accepted($"/api/investigations/{investigation.Id}", investigation);
    }
}

/// <param name="Query">
/// The question to investigate, in the user's words. Omitted, one is composed from the incident:
/// the router reads this, so a real question routes better than a generated one.
/// </param>
/// <param name="ServiceHint">
/// Which service to look at. Omitted, the incident's own service is used - which is where the
/// alert fired, not necessarily where the fault is.
/// </param>
public sealed record InvestigateBody(string? Query = null, string? ServiceHint = null);

public sealed record UpdateIncidentStatusBody(IncidentStatus Status);
