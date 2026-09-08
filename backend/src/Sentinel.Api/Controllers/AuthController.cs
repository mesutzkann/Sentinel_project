using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Features.Auth;

namespace Sentinel.Api.Controllers;

[ApiController]
[Route("api/auth")]
public sealed class AuthController : ControllerBase
{
    private readonly ISender _sender;

    public AuthController(ISender sender) => _sender = sender;

    /// <summary>
    /// Exchanges credentials for an access token. There is no signup endpoint: accounts are
    /// created by an admin, and the first admin is seeded at startup.
    /// </summary>
    [HttpPost("login")]
    [AllowAnonymous]
    [ProducesResponseType<LoginResponse>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status401Unauthorized)]
    public async Task<ActionResult<LoginResponse>> Login(
        LoginCommand command,
        CancellationToken cancellationToken) =>
        Ok(await _sender.Send(command, cancellationToken));

    /// <summary>Who the current token belongs to. The frontend calls this on load to restore a session.</summary>
    [HttpGet("me")]
    [Authorize]
    [ProducesResponseType<MeResponse>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status401Unauthorized)]
    public async Task<ActionResult<MeResponse>> Me(CancellationToken cancellationToken) =>
        Ok(await _sender.Send(new GetCurrentUserQuery(), cancellationToken));
}
