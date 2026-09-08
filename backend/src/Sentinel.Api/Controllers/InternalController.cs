using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Features.ModelPredictions;

namespace Sentinel.Api.Controllers;

/// <summary>
/// The surface the AI service calls. Not for browsers and not part of the public API.
/// </summary>
/// <remarks>
/// <see cref="AllowAnonymousAttribute"/> only turns off the user JWT; every action still goes
/// through <see cref="RequireInternalTokenAttribute"/>.
/// </remarks>
[ApiController]
[Route("internal")]
[AllowAnonymous]
[RequireInternalToken]
[ApiExplorerSettings(GroupName = "internal")]
public sealed class InternalController : ControllerBase
{
    private readonly ISender _sender;

    public InternalController(ISender sender) => _sender = sender;

    /// <summary>
    /// Records one language model call: which model, what it cost, and whether it came back as
    /// valid JSON.
    /// </summary>
    /// <remarks>
    /// The AI service posts this after every call. It is the raw material for the Phase 8 router
    /// benchmark (base against fine-tuned) and for the structured-output success rate the
    /// evaluation dashboard reports, so it is recorded for failed calls too — a call that
    /// returned unparseable JSON is exactly the data point worth keeping.
    /// </remarks>
    [HttpPost("model-predictions")]
    [ProducesResponseType<ModelPredictionDto>(StatusCodes.Status201Created)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status401Unauthorized)]
    public async Task<ActionResult<ModelPredictionDto>> RecordModelPrediction(
        RecordModelPredictionCommand command,
        CancellationToken cancellationToken)
    {
        var prediction = await _sender.Send(command, cancellationToken);
        return Created($"/api/model-predictions/{prediction.Id}", prediction);
    }
}

/// <summary>Read access to recorded model calls, for the frontend and for debugging.</summary>
[ApiController]
[Route("api/model-predictions")]
[Authorize]
public sealed class ModelPredictionsController : ControllerBase
{
    private readonly ISender _sender;

    public ModelPredictionsController(ISender sender) => _sender = sender;

    [HttpGet]
    [ProducesResponseType<IReadOnlyList<ModelPredictionDto>>(StatusCodes.Status200OK)]
    public async Task<ActionResult<IReadOnlyList<ModelPredictionDto>>> List(
        CancellationToken cancellationToken,
        [FromQuery] Guid? investigationId = null,
        [FromQuery] int limit = 50) =>
        Ok(await _sender.Send(new ListModelPredictionsQuery(investigationId, limit), cancellationToken));
}
