using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Notifications;
using Sentinel.Samples.Notifications.Domain;
using Sentinel.Samples.Notifications.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.AddSampleServiceDefaults("notifications", NotificationsChaos.All);
builder.AddSampleDbContext<NotificationsDbContext>(NotificationsDbContext.Schema);

builder.Services.AddSingleton<DeliveryGateway>();

var app = builder.Build();

await app.MigrateSampleDatabaseAsync<NotificationsDbContext>();

app.MapSampleServiceDefaults();

app.MapGet("/notifications", async (NotificationsDbContext db, int limit = 50) =>
    await db.Notifications
        .AsNoTracking()
        .OrderByDescending(n => n.CreatedAt)
        .Take(Math.Clamp(limit, 1, 200))
        .ToListAsync());

app.MapGet("/notifications/{id:guid}", async (Guid id, NotificationsDbContext db) =>
    await db.Notifications.AsNoTracking().FirstOrDefaultAsync(n => n.Id == id) is { } notification
        ? Results.Ok(notification)
        : Results.NotFound(new { message = $"Notification {id} not found." }));

app.MapPost("/notifications", async (
    SendNotificationRequest request,
    NotificationsDbContext db,
    DeliveryGateway gateway,
    ILogger<Program> logger,
    CancellationToken cancellationToken) =>
{
    if (string.IsNullOrWhiteSpace(request.Recipient))
    {
        return Results.BadRequest(new { message = "A recipient is required." });
    }

    var notification = new Notification
    {
        Recipient = request.Recipient,
        Channel = request.Channel ?? NotificationChannel.Email,
        Subject = request.Subject,
        Body = request.Body,
    };

    var delivery = await gateway.DeliverAsync(notification, cancellationToken);

    notification.Status = delivery.Succeeded ? NotificationStatus.Delivered : NotificationStatus.Failed;
    notification.FailureReason = delivery.FailureReason;

    db.Notifications.Add(notification);
    await db.SaveChangesAsync(cancellationToken);

    if (!delivery.Succeeded)
    {
        logger.LogError(
            "Notification {NotificationId} to {Recipient} failed: {Reason}",
            notification.Id, notification.Recipient, delivery.FailureReason);

        return Results.Json(
            new { id = notification.Id, status = notification.Status.ToString(), error = delivery.FailureReason },
            statusCode: StatusCodes.Status502BadGateway);
    }

    return Results.Created($"/notifications/{notification.Id}", notification);
});

app.Run();

internal sealed record SendNotificationRequest(
    string Recipient,
    string Subject,
    string Body,
    NotificationChannel? Channel);

/// <summary>
/// Chaos scenarios owned by notifications. See <c>sample-services/chaos/scenarios.md</c>.
/// Behaviour lands in Phase 2; the declarations exist now so the catalogue is queryable.
/// </summary>
internal static class NotificationsChaos
{
    public const string MemoryLeak = "MEMORY_LEAK";
    public const string WrongConnectionString = "WRONG_CONNECTION_STRING";
    public const string ExternalDependencyUnavailable = "EXTERNAL_DEPENDENCY_UNAVAILABLE";

    public static readonly ChaosScenario[] All =
    [
        new(MemoryLeak,
            "Unbounded delivery history",
            "Every delivered notification is appended to a static list that is never drained, so "
            + "working-set memory climbs monotonically until the process runs out.",
            null),

        new(WrongConnectionString,
            "Database host misconfigured",
            "The database host points at a name that does not resolve, so every database-backed "
            + "endpoint fails 100% of the time while the database itself is healthy.",
            new Dictionary<string, string> { ["host"] = "postgres-typo" }),

        new(ExternalDependencyUnavailable,
            "Delivery provider returning 503",
            "The upstream delivery provider returns 503, so notifications fail at an external "
            + "span while orders and payments stay green.",
            null),
    ];
}
