using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using Microsoft.Extensions.Options;
using Microsoft.IdentityModel.Tokens;
using Sentinel.Application.Common;
using Sentinel.Domain;

namespace Sentinel.Infrastructure.Auth;

public sealed class JwtOptions
{
    public const string SectionName = "Jwt";

    /// <summary>HMAC signing key. At least 32 bytes, or the handler refuses to sign.</summary>
    public string SigningKey { get; set; } = string.Empty;

    public string Issuer { get; set; } = "sentinel-ai";

    public string Audience { get; set; } = "sentinel-ai";

    /// <summary>
    /// Eight hours: long enough that a demo or a long investigation is not interrupted by a
    /// re-login, short enough that a leaked token is not indefinitely useful.
    /// </summary>
    public int LifetimeMinutes { get; set; } = 480;
}

public sealed class JwtTokenService : IJwtTokenService
{
    private readonly JwtOptions _options;

    public JwtTokenService(IOptions<JwtOptions> options)
    {
        _options = options.Value;

        if (Encoding.UTF8.GetByteCount(_options.SigningKey) < 32)
        {
            throw new InvalidOperationException(
                "Jwt:SigningKey must be at least 32 bytes. See .env.example.");
        }
    }

    public (string Token, DateTimeOffset ExpiresAt) CreateToken(Guid userId, string username, UserRole role)
    {
        var expiresAt = DateTimeOffset.UtcNow.AddMinutes(_options.LifetimeMinutes);

        var credentials = new SigningCredentials(
            new SymmetricSecurityKey(Encoding.UTF8.GetBytes(_options.SigningKey)),
            SecurityAlgorithms.HmacSha256);

        var token = new JwtSecurityToken(
            issuer: _options.Issuer,
            audience: _options.Audience,
            claims:
            [
                new Claim(JwtRegisteredClaimNames.Sub, userId.ToString()),
                new Claim(JwtRegisteredClaimNames.Jti, Guid.NewGuid().ToString()),
                new Claim(ClaimTypes.Name, username),
                new Claim(ClaimTypes.Role, role.ToString()),
            ],
            expires: expiresAt.UtcDateTime,
            signingCredentials: credentials);

        return (new JwtSecurityTokenHandler().WriteToken(token), expiresAt);
    }
}
