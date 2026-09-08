using System.Security.Claims;
using Microsoft.AspNetCore.Http;
using Sentinel.Application.Common;
using Sentinel.Domain;

namespace Sentinel.Infrastructure.Auth;

/// <summary>
/// Reads the caller's identity from the validated JWT on the current request.
/// </summary>
/// <remarks>
/// Every property returns null when there is no request or no token, rather than throwing.
/// Handlers that require a user say so explicitly; the AI service callback endpoint has no user
/// at all and must still work.
/// </remarks>
public sealed class CurrentUser : ICurrentUser
{
    private readonly IHttpContextAccessor _accessor;

    public CurrentUser(IHttpContextAccessor accessor) => _accessor = accessor;

    private ClaimsPrincipal? Principal => _accessor.HttpContext?.User;

    public Guid? UserId =>
        Guid.TryParse(Principal?.FindFirstValue(ClaimTypes.NameIdentifier), out var id) ? id : null;

    public string? Username => Principal?.FindFirstValue(ClaimTypes.Name);

    public UserRole? Role =>
        Enum.TryParse<UserRole>(Principal?.FindFirstValue(ClaimTypes.Role), out var role) ? role : null;
}

/// <summary>BCrypt, with the library's default work factor.</summary>
public sealed class BCryptPasswordHasher : IPasswordHasher
{
    public string Hash(string password) => BCrypt.Net.BCrypt.HashPassword(password);

    public bool Verify(string password, string hash)
    {
        try
        {
            return BCrypt.Net.BCrypt.Verify(password, hash);
        }
        catch (BCrypt.Net.SaltParseException)
        {
            // A malformed hash in the database is a failed verification, not a crash. It can
            // happen if a row was written by hand.
            return false;
        }
    }
}
