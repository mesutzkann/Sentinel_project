namespace Sentinel.Samples.Notifications.Domain;

public enum NotificationChannel
{
    Email,
    Sms,
    Push,
}

public enum NotificationStatus
{
    Queued,
    Delivered,
    Failed,
}

public sealed class Notification
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public required string Recipient { get; set; }

    public NotificationChannel Channel { get; set; } = NotificationChannel.Email;

    public required string Subject { get; set; }

    public required string Body { get; set; }

    public NotificationStatus Status { get; set; } = NotificationStatus.Queued;

    /// <summary>Why delivery failed. Null on success — this is the evidence scenario 12 leaves behind.</summary>
    public string? FailureReason { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}
