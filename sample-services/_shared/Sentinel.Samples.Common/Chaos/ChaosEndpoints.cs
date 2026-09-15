using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.DependencyInjection;
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

        group.MapPost("/{code}/enable", async (
            string code,
            Dictionary<string, string>? parameters,
            ChaosRegistry registry,
            IEnumerable<IChaosActivationHandler> handlers,
            ILogger<ChaosRegistry> logger,
            CancellationToken cancellationToken) =>
        {
            if (!registry.Enable(code, parameters))
            {
                return UnknownScenario(code, registry);
            }

            // Logged at Warning so it stands out in Loki. An investigation that finds this line
            // has effectively found the answer, which is why agent evaluation reads only the
            // service's own telemetry and never this endpoint.
            logger.LogWarning("Chaos scenario {ChaosCode} enabled", code);

            // Awaited rather than backgrounded: the scenario is only reproducible once its
            // setup has finished, so enable must not return before then.
            foreach (var handler in handlers)
            {
                await handler.OnEnabledAsync(code, cancellationToken);
            }

            return Results.Ok(registry.Snapshot());
        }).WithName("EnableChaosScenario");

        group.MapPost("/{code}/disable", async (
            string code,
            ChaosRegistry registry,
            IEnumerable<IChaosActivationHandler> handlers,
            ILogger<ChaosRegistry> logger,
            CancellationToken cancellationToken) =>
        {
            if (!registry.Disable(code))
            {
                return UnknownScenario(code, registry);
            }

            logger.LogWarning("Chaos scenario {ChaosCode} disabled", code);

            // Awaited for the same reason enable is: a scenario that reached outside this process
            // is not off until whatever it reached has been told so, and the caller is entitled
            // to assume a returned disable means disabled.
            foreach (var handler in handlers)
            {
                await handler.OnDisabledAsync(code, cancellationToken);
            }

            return Results.Ok(registry.Snapshot());
        }).WithName("DisableChaosScenario");

        group.MapPost("/reset", async (
            ChaosRegistry registry,
            IEnumerable<IChaosActivationHandler> handlers,
            ILogger<ChaosRegistry> logger,
            CancellationToken cancellationToken) =>
        {
            // Snapshot before clearing: a handler has to be told which scenarios it is being
            // asked to undo, and after Reset the registry no longer knows which were on.
            var wasEnabled = registry.Snapshot()
                .Where(state => state.Enabled)
                .Select(state => state.Code)
                .ToArray();

            registry.Reset();
            logger.LogWarning("All chaos scenarios reset");

            foreach (var code in wasEnabled)
            {
                foreach (var handler in handlers)
                {
                    await handler.OnDisabledAsync(code, cancellationToken);
                }
            }

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
