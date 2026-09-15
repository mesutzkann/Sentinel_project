using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Gateway;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("gateway", GatewayChaos.All);

// The edge of the system: no database of its own, it only fans out to the other four.
// That makes it the natural place for chaos scenario 8 (TIMEOUT_TOO_LOW), where the caller
// fails while the callee succeeds.
builder.Services.AddHttpClient<DownstreamClient>(client =>
{
    client.Timeout = TimeSpan.FromSeconds(30);
});

var app = builder.Build();

app.MapSampleServiceDefaults();

// Runs the whole flow: look up the user, create the order, return what happened.
app.MapPost("/api/checkout", async (
    CheckoutRequest request,
    DownstreamClient downstream,
    ILogger<Program> logger,
    CancellationToken cancellationToken) =>
{
    var user = await downstream.GetUserAsync(request.UserId, cancellationToken);
    if (user is null)
    {
        return Results.NotFound(new { message = $"User {request.UserId} not found." });
    }

    logger.LogInformation("Checkout started for {UserId}", request.UserId);

    var result = await downstream.CreateOrderAsync(
        request.UserId, request.Items, request.Currency, cancellationToken);

    return result.StatusCode is >= 200 and < 300
        ? Results.Json(result.Body, statusCode: result.StatusCode)
        : Results.Json(result.Body, statusCode: result.StatusCode);
});

app.MapGet("/api/orders/{id:guid}", async (
    Guid id,
    DownstreamClient downstream,
    CancellationToken cancellationToken) =>
{
    var order = await downstream.GetOrderAsync(id, cancellationToken);

    return order is null
        ? Results.NotFound(new { message = $"Order {id} not found." })
        : Results.Json(order);
});

// Aggregated health of the whole sample estate. The backend's service dashboard polls this in
// Phase 1, before Prometheus exists.
app.MapGet("/api/services/health", async (
    DownstreamClient downstream,
    CancellationToken cancellationToken) =>
    Results.Ok(await downstream.GetEstateHealthAsync(cancellationToken)));

app.Run();

internal sealed record CheckoutRequest(
    Guid UserId,
    List<CheckoutItem> Items,
    string? Currency);

internal sealed record CheckoutItem(string ProductName, int Quantity, decimal UnitPrice);

/// <summary>
/// Chaos scenarios owned by the gateway. See <c>sample-services/chaos/scenarios.md</c>.
/// Behaviour lands in Phase 2; the declarations exist now so the catalogue is queryable.
/// </summary>
internal static class GatewayChaos
{
    public const string TimeoutTooLow = ChaosCodes.TimeoutTooLow;

    public static readonly ChaosScenario[] All =
    [
        new(TimeoutTooLow,
            "Downstream timeout set too low",
            "The deadline on the checkout call drops from 30s to 40ms, below what the call "
            + "legitimately takes, so the gateway gives up on work the services below it finish.",
            // 40ms against a checkout that takes about 80ms warm. The catalogue's original
            // numbers - 500ms against an 800ms call - describe the same mistake at a scale this
            // stack does not have: nothing here legitimately takes 800ms, so a 500ms deadline
            // would never fire and the scenario would be a no-op.
            new Dictionary<string, string> { ["timeout_ms"] = "40" }),
    ];
}
