using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Orders;
using Sentinel.Samples.Orders.Domain;
using Sentinel.Samples.Orders.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("orders", OrdersChaos.All);
builder.AddSampleDbContext<OrdersDbContext>(OrdersDbContext.Schema);

builder.Services.AddSingleton<ChaosBehaviour>();
builder.Services.AddSingleton<IChaosActivationHandler>(sp => sp.GetRequiredService<ChaosBehaviour>());

builder.Services.AddHttpClient<PaymentsClient>(client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["Services:Payments"] ?? "http://localhost:8083");
    client.Timeout = TimeSpan.FromSeconds(30);
});

var app = builder.Build();

await app.MigrateSampleDatabaseAsync<OrdersDbContext>();

app.MapSampleServiceDefaults();

app.MapGet("/orders", async (
    OrdersDbContext db,
    ChaosBehaviour chaos,
    CancellationToken cancellationToken,
    Guid? userId = null,
    int limit = 50) =>
{
    var take = Math.Clamp(limit, 1, 200);

    // Chaos scenario 15 (CPU_SATURATION). Before the read rather than instead of it: the point
    // is that the database work is untouched and still fast, so the trace shows the time sitting
    // in a compute span beside a healthy query rather than in the query.
    if (chaos.IsEnabled(ChaosCodes.CpuSaturation))
    {
        chaos.BurnCpu();
    }

    // Chaos scenario 2 (DB_SLOW_QUERY_MISSING_INDEX). The healthy path below stays on its
    // indexes over the same table, which is what makes the contrast measurable.
    if (chaos.IsEnabled(ChaosCodes.DbSlowQueryMissingIndex))
    {
        return Results.Ok(await chaos.ListSlowlyAsync(db, take, cancellationToken));
    }

    // Chaos scenario 1 (DB_CONNECTION_POOL_EXHAUSTION). The shared data access layer has already
    // shrunk the pool; this is the half that holds a connection long enough for it to matter.
    if (chaos.IsEnabled(ChaosCodes.DbConnectionPoolExhaustion))
    {
        return Results.Ok(await chaos.ListHoldingConnectionAsync(db, take, cancellationToken));
    }

    var query = db.Orders.AsNoTracking();

    if (userId is { } id)
    {
        query = query.Where(o => o.UserId == id);
    }

    return Results.Ok(await query
        .OrderByDescending(o => o.CreatedAt)
        .Take(take)
        .ToListAsync(cancellationToken));
});

app.MapGet("/orders/{id:guid}", async (
    Guid id,
    OrdersDbContext db,
    ChaosBehaviour chaos,
    CancellationToken cancellationToken) =>
{
    // Eager loading is the correct shape. Chaos scenario 4 (DB_N_PLUS_ONE_QUERY) drops the
    // Include at runtime, turning one query into one-per-line-item.
    var order = chaos.IsEnabled(ChaosCodes.DbNPlusOneQuery)
        ? await chaos.GetWithNPlusOneAsync(db, id, cancellationToken)
        : await db.Orders
            .AsNoTracking()
            .Include(o => o.Items)
            .FirstOrDefaultAsync(o => o.Id == id, cancellationToken);

    return order is null
        ? Results.NotFound(new { message = $"Order {id} not found." })
        : Results.Ok(order);
});

app.MapPost("/orders", async (
    CreateOrderRequest request,
    OrdersDbContext db,
    ChaosBehaviour chaos,
    PaymentsClient payments,
    ILogger<Program> logger,
    CancellationToken cancellationToken) =>
{
    if (request.Items is null || request.Items.Count == 0)
    {
        return Results.BadRequest(new { message = "An order needs at least one item." });
    }

    // Chaos scenario 6 (DIVIDE_BY_ZERO_EDGE_CASE) removes this guard, which is the defect: the
    // pricing below divides a campaign discount by the line's quantity. Rejecting the input is
    // the catalogue's fix for the scenario, so the healthy path is the fixed one.
    if (!chaos.IsEnabled(ChaosCodes.DivideByZeroEdgeCase)
        && request.Items.Any(i => i.Quantity < 1))
    {
        return Results.BadRequest(new { message = "Every item needs a quantity of at least one." });
    }

    var order = new Order
    {
        UserId = request.UserId,
        Currency = request.Currency ?? "TRY",
        Items = request.Items
            .Select(i => new OrderItem
            {
                ProductName = i.ProductName,
                Quantity = i.Quantity,
                UnitPrice = i.UnitPrice,
            })
            .ToList(),
    };

    // Same scenario 6. The discounted pricing is what actually divides; with the scenario off it
    // is never reached, so the healthy path keeps the plain sum it always had.
    order.TotalAmount = chaos.IsEnabled(ChaosCodes.DivideByZeroEdgeCase)
        ? ChaosBehaviour.PriceWithPerUnitDiscount(order)
        : order.Items.Sum(i => i.LineTotal);

    db.Orders.Add(order);
    await db.SaveChangesAsync(cancellationToken);

    var authorization = await payments.AuthorizeAsync(
        order.Id, order.TotalAmount, order.Currency, cancellationToken);

    if (authorization is null || !string.Equals(authorization.Status, "authorized", StringComparison.OrdinalIgnoreCase))
    {
        order.Status = OrderStatus.PaymentFailed;
        await db.SaveChangesAsync(cancellationToken);

        logger.LogWarning("Order {OrderId} could not be paid", order.Id);
        return Results.Json(
            new { order_id = order.Id, status = order.Status.ToString(), message = "Payment declined." },
            statusCode: StatusCodes.Status402PaymentRequired);
    }

    order.Status = OrderStatus.Paid;
    order.PaymentReference = authorization.ProviderReference;
    await db.SaveChangesAsync(cancellationToken);

    logger.LogInformation(
        "Order {OrderId} paid, total {Total} {Currency}",
        order.Id, order.TotalAmount, order.Currency);

    return Results.Created($"/orders/{order.Id}", order);
});

app.Run();

internal sealed record CreateOrderRequest(
    Guid UserId,
    List<CreateOrderItem> Items,
    string? Currency);

internal sealed record CreateOrderItem(string ProductName, int Quantity, decimal UnitPrice);

/// <summary>
/// Chaos scenarios owned by orders. See <c>sample-services/chaos/scenarios.md</c>.
/// Behaviour lands in Phase 2; the declarations exist now so the catalogue is queryable.
/// </summary>
internal static class OrdersChaos
{
    public const string ConnectionPoolExhaustion = ChaosCodes.DbConnectionPoolExhaustion;
    public const string SlowQueryMissingIndex = ChaosCodes.DbSlowQueryMissingIndex;
    public const string NPlusOneQuery = ChaosCodes.DbNPlusOneQuery;
    public const string DivideByZeroEdgeCase = ChaosCodes.DivideByZeroEdgeCase;
    public const string CpuSaturation = ChaosCodes.CpuSaturation;

    public static readonly ChaosScenario[] All =
    [
        new(ConnectionPoolExhaustion,
            "Connection pool exhaustion",
            "Npgsql max pool size drops from 200 to 20 while load continues, so requests queue "
            + "waiting for a connection and eventually time out.",
            new Dictionary<string, string>
            {
                ["max_pool_size"] = "20",
                // How long each request holds its connection. Exhaustion is a function of
                // concurrency times hold time against pool size; without a hold, shrinking the
                // pool changes nothing observable.
                ["hold_ms"] = "1000",
                // The ceiling latency climbs to before a request gives up. Short enough that
                // an evaluation run does not sit on the 15 second Npgsql default, and low
                // enough that queueing actually breaches it at realistic concurrency.
                ["timeout_seconds"] = "3",
            }),

        new(SlowQueryMissingIndex,
            "Slow query, missing index",
            "Order lookup switches to a non-indexed predicate, forcing a sequential scan. "
            + "Latency rises with no errors at all.",
            new Dictionary<string, string> { ["row_count"] = "2000000" }),

        new(NPlusOneQuery,
            "N+1 query on order detail",
            "Order detail stops eager-loading its items and issues one query per line item. "
            + "Many fast queries instead of one slow one.",
            null),

        new(DivideByZeroEdgeCase,
            "Divide by zero in discount calculation",
            "Per-item discount divides by item count and quantity zero is accepted, throwing "
            + "on a small subset of requests.",
            null),

        new(CpuSaturation,
            "CPU saturation from a hot loop",
            "An expensive hashing loop runs on every request. Latency rises with no database "
            + "or network involvement.",
            new Dictionary<string, string> { ["iterations"] = "150000" }),
    ];
}
