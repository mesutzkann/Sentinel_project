using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Features.Services;
using Sentinel.Infrastructure;

namespace Sentinel.Api.Controllers;

[ApiController]
[Route("api/services")]
[Authorize]
public sealed class ServicesController : ControllerBase
{
    private readonly ISender _sender;

    public ServicesController(ISender sender) => _sender = sender;

    [HttpGet]
    [ProducesResponseType<IReadOnlyList<ServiceDto>>(StatusCodes.Status200OK)]
    public async Task<ActionResult<IReadOnlyList<ServiceDto>>> List(CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new ListServicesQuery(), cancellationToken));

    [HttpGet("{id:guid}")]
    [ProducesResponseType<ServiceDto>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<ServiceDto>> Get(Guid id, CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new GetServiceQuery(id), cancellationToken));

    [HttpPost]
    [Authorize(Policy = Policies.RequireAdmin)]
    [ProducesResponseType<ServiceDto>(StatusCodes.Status201Created)]
    [ProducesResponseType(StatusCodes.Status409Conflict)]
    public async Task<ActionResult<ServiceDto>> Create(
        CreateServiceCommand command,
        CancellationToken cancellationToken)
    {
        var service = await _sender.Send(command, cancellationToken);
        return CreatedAtAction(nameof(Get), new { id = service.Id }, service);
    }

    [HttpPut("{id:guid}")]
    [Authorize(Policy = Policies.RequireAdmin)]
    [ProducesResponseType<ServiceDto>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<ActionResult<ServiceDto>> Update(
        Guid id,
        UpdateServiceBody body,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(
            new UpdateServiceCommand(id, body.DisplayName, body.RepoPath, body.HealthUrl, body.MetricsJob),
            cancellationToken));

    [HttpDelete("{id:guid}")]
    [Authorize(Policy = Policies.RequireAdmin)]
    [ProducesResponseType(StatusCodes.Status204NoContent)]
    [ProducesResponseType(StatusCodes.Status409Conflict)]
    public async Task<IActionResult> Delete(Guid id, CancellationToken cancellationToken)
    {
        await _sender.Send(new DeleteServiceCommand(id), cancellationToken);
        return NoContent();
    }
}

/// <summary>Update body without the id, which comes from the route.</summary>
public sealed record UpdateServiceBody(
    string DisplayName,
    string? RepoPath,
    string? HealthUrl,
    string? MetricsJob);
