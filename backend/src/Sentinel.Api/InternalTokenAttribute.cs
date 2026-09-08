using System.Security.Cryptography;
using System.Text;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;

namespace Sentinel.Api;

/// <summary>
/// Requires a shared secret in <c>X-Internal-Token</c>.
/// </summary>
/// <remarks>
/// The <c>/internal</c> surface is called by the AI service, not by a person, so a user JWT is
/// the wrong shape: there is no user, and minting one for a background service would put a
/// long-lived credential with a role on it into another process. A shared secret carried in a
/// header is the smaller thing to get right, and the same secret becomes the
/// <c>callback_token</c> the investigation callbacks in Phase 7 use.
///
/// This is not a substitute for the endpoint being unreachable from outside: every port is
/// published on loopback, and the AI service reaches the backend over the compose network.
/// </remarks>
[AttributeUsage(AttributeTargets.Class | AttributeTargets.Method)]
public sealed class RequireInternalTokenAttribute : Attribute, IAsyncActionFilter
{
    public const string HeaderName = "X-Internal-Token";

    /// <summary>Configuration key holding the expected value.</summary>
    public const string ConfigurationKey = "Internal:ApiKey";

    /// <summary>Flat environment variable carrying the same secret, shared with the AI service.</summary>
    public const string EnvironmentKey = "INTERNAL_API_KEY";

    public Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        var configuration = context.HttpContext.RequestServices
            .GetRequiredService<IConfiguration>();

        // INTERNAL_API_KEY is the fallback so the AI service and the backend can read the same
        // variable out of the repository .env; ASP.NET would otherwise only bind Internal__ApiKey
        // and the two would drift apart at the point where the AI service starts being rejected.
        var expected = configuration[ConfigurationKey] ?? configuration[EnvironmentKey];

        if (string.IsNullOrWhiteSpace(expected))
        {
            // Refused rather than allowed. An unset secret must not silently turn the internal
            // surface into an open one.
            context.Result = new ObjectResult(new
            {
                message = $"{ConfigurationKey} is not configured, so the internal API is closed.",
            })
            {
                StatusCode = StatusCodes.Status503ServiceUnavailable,
            };

            return Task.CompletedTask;
        }

        var provided = context.HttpContext.Request.Headers[HeaderName].ToString();

        if (!FixedTimeEquals(provided, expected))
        {
            context.Result = new UnauthorizedObjectResult(new
            {
                message = $"A valid {HeaderName} header is required.",
            });

            return Task.CompletedTask;
        }

        return next();
    }

    /// <summary>
    /// Compares in time independent of how many leading characters match, so the comparison
    /// itself does not leak the secret one character at a time.
    /// </summary>
    private static bool FixedTimeEquals(string provided, string expected) =>
        CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(provided),
            Encoding.UTF8.GetBytes(expected));
}
