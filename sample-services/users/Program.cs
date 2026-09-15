using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Users;
using Sentinel.Samples.Users.Domain;
using Sentinel.Samples.Users.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("users", UsersChaos.All);
builder.AddSampleDbContext<UsersDbContext>(UsersDbContext.Schema);

// The one service users calls. It exists for the profile endpoint below, and it is what chaos
// scenario 10 (RETRY_STORM) amplifies.
builder.Services.AddHttpClient<OrdersClient>(client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["Services:Orders"] ?? "http://localhost:8082");
    client.Timeout = TimeSpan.FromSeconds(30);
});

var app = builder.Build();

await app.MigrateSampleDatabaseAsync<UsersDbContext>();
await SeedData.EnsureSeededAsync(app);

app.MapSampleServiceDefaults();

app.MapGet("/users", async (UsersDbContext db, int limit = 50) =>
    await db.Users
        .OrderByDescending(u => u.CreatedAt)
        .Take(Math.Clamp(limit, 1, 200))
        .ToListAsync());

app.MapGet("/users/{id:guid}", async (Guid id, UsersDbContext db) =>
    await db.Users.FindAsync(id) is { } user
        ? Results.Ok(user)
        : Results.NotFound(new { message = $"User {id} not found." }));

// A user with their recent orders, which is the read that makes users a caller at all.
app.MapGet("/users/{id:guid}/orders", async (
    Guid id,
    UsersDbContext db,
    OrdersClient orders,
    CancellationToken cancellationToken) =>
{
    if (await db.Users.FindAsync([id], cancellationToken) is null)
    {
        return Results.NotFound(new { message = $"User {id} not found." });
    }

    return Results.Ok(new
    {
        user_id = id,
        orders = await orders.GetForUserAsync(id, cancellationToken),
    });
});

app.MapPost("/users", async (CreateUserRequest request, UsersDbContext db) =>
{
    if (string.IsNullOrWhiteSpace(request.Email) || !request.Email.Contains('@'))
    {
        return Results.BadRequest(new { message = "A valid email is required." });
    }

    if (await db.Users.AnyAsync(u => u.Email == request.Email))
    {
        return Results.Conflict(new { message = $"Email {request.Email} is already registered." });
    }

    var user = new User { Email = request.Email, FullName = request.FullName };
    db.Users.Add(user);
    await db.SaveChangesAsync();

    return Results.Created($"/users/{user.Id}", user);
});

app.Run();

internal sealed record CreateUserRequest(string Email, string FullName);

/// <summary>
/// Chaos scenarios owned by this service. See <c>sample-services/chaos/scenarios.md</c>.
/// Behaviour lands in Phase 2; the declarations exist now so the catalogue is queryable
/// from the moment the service starts.
/// </summary>
internal static class UsersChaos
{
    public const string RetryStorm = ChaosCodes.RetryStorm;

    public static readonly ChaosScenario[] All =
    [
        new(RetryStorm,
            "Retry storm against orders",
            "Retry policy is set to 10 attempts with no backoff, amplifying downstream load "
            + "roughly tenfold while inbound traffic to users is unchanged.",
            new Dictionary<string, string> { ["attempts"] = "10", ["backoff_ms"] = "0" }),
    ];
}
