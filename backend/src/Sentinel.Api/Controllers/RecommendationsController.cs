using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Common;
using Sentinel.Application.Features.Auth;
using Sentinel.Application.Features.Recommendations;
using Sentinel.Infrastructure;

namespace Sentinel.Api.Controllers;

/// <summary>
/// The human in the loop: approving a proposed fix, or refusing it.
/// </summary>
/// <remarks>
/// <para>
/// Engineers only, and the approver is taken from the token rather than from the body. Who
/// approved a change to a running system is the one field on that row nobody may supply.
/// </para>
/// <para>
/// Approving is slow on purpose. The request runs the tool, waits for the service to settle and
/// re-measures the symptom before answering, so a caller sees the verdict in the response rather
/// than having to poll for it. Budget up to three minutes.
/// </para>
/// </remarks>
[ApiController]
[Route("api/recommendations")]
[Authorize]
public sealed class RecommendationsController : ControllerBase
{
    private readonly ISender _sender;
    private readonly ICurrentUser _user;

    public RecommendationsController(ISender sender, ICurrentUser user)
    {
        _sender = sender;
        _user = user;
    }

    /// <summary>Approves a recommendation and runs it.</summary>
    [HttpPost("{id:guid}/approve")]
    [Authorize(Policy = Policies.RequireEngineer)]
    [ProducesResponseType<RecommendationOutcome>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    [ProducesResponseType(StatusCodes.Status409Conflict)]
    [ProducesResponseType(StatusCodes.Status502BadGateway)]
    public async Task<ActionResult<RecommendationOutcome>> Approve(
        Guid id,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new ApproveRecommendationCommand(id, Approver()), cancellationToken));

    /// <summary>Refuses a recommendation, with a reason worth keeping.</summary>
    /// <remarks>
    /// The reason is stored: "the pool size is deliberate, it is sized for the batch job" is
    /// exactly what the next investigation into the same symptom should be able to read.
    /// </remarks>
    [HttpPost("{id:guid}/reject")]
    [Authorize(Policy = Policies.RequireEngineer)]
    [ProducesResponseType<RecommendationOutcome>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    [ProducesResponseType(StatusCodes.Status409Conflict)]
    public async Task<ActionResult<RecommendationOutcome>> Reject(
        Guid id,
        RejectBody? body,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(
            new RejectRecommendationCommand(id, Approver(), body?.Reason), cancellationToken));

    /// <summary>
    /// The approver, from the authenticated principal.
    /// </summary>
    /// <remarks>
    /// Never from the request body. An audit trail that records the name the caller typed is not
    /// an audit trail, and this row is the only record of who authorised a change to a running
    /// service.
    /// </remarks>
    private Guid Approver() =>
        _user.UserId ?? throw new UnauthorizedException("The approver could not be identified.");
}

public sealed record RejectBody(string? Reason);
