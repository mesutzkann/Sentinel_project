using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Features.Incidents;
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
}

public sealed record UpdateIncidentStatusBody(IncidentStatus Status);
