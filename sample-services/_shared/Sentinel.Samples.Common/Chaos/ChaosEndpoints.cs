using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Logging;

namespace Sentinel.Samples.Common.Chaos;

/// <summary>
/// The control surface agent evaluation uses to reproduce incidents on demand.
/// Contract is documented in <c>sample-services/chaos/scenarios.md</c>.
/// </summary>
public static class ChaosEndpoints
{
    public static IEndpointRouteBuilder MapChaosEndpoints(this IEndpointRouteBuilder app)
    {
        var group = app.MapGroup("/chaos").WithTags("Chaos");

        group.MapGet("/", (ChaosRegistry registry) => Results.Ok(registry.Snapshot()))
            .WithName("ListChaosScenarios");

        group.MapPost("/{code}/enable", (
            string code,
            Dictionary<string, string>? parameters,
            ChaosRegistry registry,
            ILogger<ChaosRegistry> logger) =>
        {
            if (!registry.Enable(code, parameters))
            {
                return UnknownScenario(code, registry);
            }

            // Logged at Warning so it stands out in Loki. An investigation that finds this line
            // has effectively found the answer, which is why agent evaluation reads only the
            // service's own telemetry and never this endpoint.
            logger.LogWarning("Chaos scenario {ChaosCode} enabled", code);
            return Results.Ok(registry.Snapshot());
        }).WithName("EnableChaosScenario");

        group.MapPost("/{code}/disable", (
            string code,
            ChaosRegistry registry,
            ILogger<ChaosRegistry> logger) =>
        {
            if (!registry.Disable(code))
            {
                return UnknownScenario(code, registry);
            }

            logger.LogWarning("Chaos scenario {ChaosCode} disabled", code);
            return Results.Ok(registry.Snapshot());
        }).WithName("DisableChaosScenario");

        group.MapPost("/reset", (ChaosRegistry registry, ILogger<ChaosRegistry> logger) =>
        {
            registry.Reset();
            logger.LogWarning("All chaos scenarios reset");
            return Results.Ok(registry.Snapshot());
        }).WithName("ResetChaosScenarios");

        return app;
    }

    private static IResult UnknownScenario(string code, ChaosRegistry registry) =>
        Results.Problem(
            title: "Unknown chaos scenario",
            detail: $"This service does not own '{code}'. It owns: "
                    + string.Join(", ", registry.Declared.Select(s => s.Code).Order(StringComparer.Ordinal)),
            statusCode: StatusCodes.Status404NotFound);
}
