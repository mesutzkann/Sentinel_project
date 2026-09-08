using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Orders;
using Sentinel.Samples.Orders.Domain;
using Sentinel.Samples.Orders.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("orders", OrdersChaos.All);
builder.AddSampleDbContext<OrdersDbContext>(OrdersDbContext.Schema);

builder.Services.AddHttpClient<PaymentsClient>(client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["Services:Payments"] ?? "http://localhost:8083");
    client.Timeout = TimeSpan.FromSeconds(30);
});

var app = builder.Build();

await app.MigrateSampleDatabaseAsync<OrdersDbContext>();

app.MapSampleServiceDefaults();

app.MapGet("/orders", async (OrdersDbContext db, Guid? userId, int limit = 50) =>
{
    var query = db.Orders.AsNoTracking();

    if (userId is { } id)
    {
        query = query.Where(o => o.UserId == id);
    }

    return await query
        .OrderByDescending(o => o.CreatedAt)
        .Take(Math.Clamp(limit, 1, 200))
        .ToListAsync();
});

app.MapGet("/orders/{id:guid}", async (Guid id, OrdersDbContext db) =>
{
    // Eager loading is the correct shape. Chaos scenario 4 (DB_N_PLUS_ONE_QUERY) drops the
    // Include at runtime, turning one query into one-per-line-item.
    var order = await db.Orders
        .AsNoTracking()
        .Include(o => o.Items)
        .FirstOrDefaultAsync(o => o.Id == id);

    return order is null
        ? Results.NotFound(new { message = $"Order {id} not found." })
        : Results.Ok(order);
});

app.MapPost("/orders", async (
    CreateOrderRequest request,
    OrdersDbContext db,
    PaymentsClient payments,
    ILogger<Program> logger,
    CancellationToken cancellationToken) =>
{
    if (request.Items is null || request.Items.Count == 0)
    {
        return Results.BadRequest(new { message = "An order needs at least one item." });
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

    order.TotalAmount = order.Items.Sum(i => i.LineTotal);

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
    public const string ConnectionPoolExhaustion = "DB_CONNECTION_POOL_EXHAUSTION";
    public const string SlowQueryMissingIndex = "DB_SLOW_QUERY_MISSING_INDEX";
    public const string NPlusOneQuery = "DB_N_PLUS_ONE_QUERY";
    public const string DivideByZeroEdgeCase = "DIVIDE_BY_ZERO_EDGE_CASE";
    public const string CpuSaturation = "CPU_SATURATION";

    public static readonly ChaosScenario[] All =
    [
        new(ConnectionPoolExhaustion,
            "Connection pool exhaustion",
            "Npgsql max pool size drops from 200 to 20 while load continues, so requests queue "
            + "waiting for a connection and eventually time out.",
            new Dictionary<string, string> { ["max_pool_size"] = "20" }),

        new(SlowQueryMissingIndex,
            "Slow query, missing index",
            "Order lookup switches to a non-indexed predicate, forcing a sequential scan. "
            + "Latency rises with no errors at all.",
            new Dictionary<string, string> { ["row_count"] = "200000" }),

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
