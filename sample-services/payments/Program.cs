using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Payments;
using Sentinel.Samples.Payments.Domain;
using Sentinel.Samples.Payments.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("payments", PaymentsChaos.All);
builder.AddSampleDbContext<PaymentsDbContext>(PaymentsDbContext.Schema);

builder.Services.AddSingleton<PaymentProcessor>();
builder.Services.AddSingleton<ChaosBehaviour>();

builder.Services.AddHttpClient<NotificationsClient>(client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["Services:Notifications"] ?? "http://localhost:8084");
    client.Timeout = TimeSpan.FromSeconds(30);
});

var app = builder.Build();

await app.MigrateSampleDatabaseAsync<PaymentsDbContext>();
await SeedData.EnsureSeededAsync(app);

app.MapSampleServiceDefaults();

app.MapGet("/payments", async (PaymentsDbContext db, Guid? orderId, int limit = 50) =>
{
    var query = db.Payments.AsNoTracking();

    if (orderId is { } id)
    {
        query = query.Where(p => p.OrderId == id);
    }

    return await query
        .OrderByDescending(p => p.CreatedAt)
        .Take(Math.Clamp(limit, 1, 200))
        .ToListAsync();
});

app.MapGet("/payments/{id:guid}", async (Guid id, PaymentsDbContext db) =>
    await db.Payments.AsNoTracking().FirstOrDefaultAsync(p => p.Id == id) is { } payment
        ? Results.Ok(payment)
        : Results.NotFound(new { message = $"Payment {id} not found." }));

app.MapPost("/payments/authorize", async (
    AuthorizeRequest request,
    PaymentsDbContext db,
    PaymentProcessor processor,
    ChaosBehaviour chaos,
    ChaosRegistry chaosRegistry,
    NotificationsClient notifications,
    ILogger<Program> logger,
    CancellationToken cancellationToken) =>
{
    // Chaos scenario 3 (DB_DEADLOCK). Runs before the payment is written so a lost deadlock
    // fails the request outright, the way a real one would.
    if (chaosRegistry.IsEnabled(ChaosCodes.DbDeadlock))
    {
        await chaos.ContendForBalancesAsync(db, cancellationToken);
    }

    var payment = processor.Authorize(request.OrderId, request.Amount, request.Currency);

    db.Payments.Add(payment);
    await db.SaveChangesAsync(cancellationToken);

    logger.LogInformation(
        "Payment {PaymentId} for order {OrderId}: {Status}",
        payment.Id, payment.OrderId, payment.Status);

    if (payment.Status == PaymentStatus.Authorized)
    {
        // Fire-and-forget would hide chaos scenarios 7, 9 and 12, which all live in
        // notifications. Awaiting keeps the failure visible in this service's traces.
        await notifications.SendPaymentConfirmationAsync(payment, cancellationToken);
    }

    return Results.Ok(new
    {
        id = payment.Id,
        status = payment.Status.ToString().ToLowerInvariant(),
        provider_reference = payment.ProviderReference,
    });
});

app.Run();

internal sealed record AuthorizeRequest(Guid OrderId, decimal Amount, string Currency);

/// <summary>
/// Chaos scenarios owned by payments. See <c>sample-services/chaos/scenarios.md</c>.
/// Behaviour lands in Phase 2; the declarations exist now so the catalogue is queryable.
/// </summary>
internal static class PaymentsChaos
{
    public const string Deadlock = ChaosCodes.DbDeadlock;
    public const string NullReference = ChaosCodes.NullReferenceException;
    public const string DownstreamLatencyCascade = ChaosCodes.DownstreamLatencyCascade;
    public const string CircuitBreakerStuckOpen = ChaosCodes.CircuitBreakerStuckOpen;
    public const string BadDeploymentRegression = ChaosCodes.BadDeploymentRegression;

    public static readonly ChaosScenario[] All =
    [
        new(Deadlock,
            "Deadlock between payment and balance updates",
            "Two concurrent authorisations lock the payment row and the merchant balance row in "
            + "opposite order, producing intermittent 40P01 errors that recover on their own.",
            null),

        new(NullReference,
            "Null reference on unmapped currency",
            "The currency guard in PaymentProcessor is bypassed, so an unmapped currency "
            + "dereferences null and throws on a subset of requests without affecting latency.",
            null),

        new(DownstreamLatencyCascade,
            "Slow authorisation cascading upstream",
            "A multi-second delay in the authorisation path pushes p99 up in orders and gateway "
            + "as well, while payments itself logs nothing.",
            new Dictionary<string, string> { ["delay_ms"] = "3000" }),

        new(CircuitBreakerStuckOpen,
            "Circuit breaker stuck open",
            "The breaker opens after a burst of failures and the half-open probe never succeeds, "
            + "so requests fail in milliseconds: high error rate with unusually low latency.",
            new Dictionary<string, string> { ["failure_threshold"] = "5" }),

        new(BadDeploymentRegression,
            "Regression introduced by a deployment",
            "Marks a known bad commit as deployed now, so the error rate steps up at a timestamp "
            + "that lines up with a commit in the repository.",
            null),
    ];
}
