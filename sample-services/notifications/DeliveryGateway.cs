using Sentinel.Samples.Notifications.Domain;

namespace Sentinel.Samples.Notifications;

/// <summary>
/// Stands in for an external delivery provider (SMTP, SMS gateway, push service).
/// </summary>
/// <remarks>
/// Nothing leaves the process — it just costs a little time and always succeeds. That healthy
/// baseline is the point: chaos scenario 12 (EXTERNAL_DEPENDENCY_UNAVAILABLE) makes it return
/// 503 instead, and the contrast is what the agent detects.
/// </remarks>
public sealed class DeliveryGateway
{
    private readonly ILogger<DeliveryGateway> _logger;

    public DeliveryGateway(ILogger<DeliveryGateway> logger) => _logger = logger;

    public async Task<DeliveryResult> DeliverAsync(
        Notification notification,
        CancellationToken cancellationToken)
    {
        // A realistic provider is not instant. Without this the service is so fast that latency
        // regressions elsewhere have nothing to stand out against.
        await Task.Delay(Random.Shared.Next(15, 45), cancellationToken);

        _logger.LogInformation(
            "Delivered {Channel} notification to {Recipient}",
            notification.Channel,
            notification.Recipient);

        return DeliveryResult.Success;
    }
}

public sealed record DeliveryResult(bool Succeeded, string? FailureReason)
{
    public static readonly DeliveryResult Success = new(true, null);

    public static DeliveryResult Failed(string reason) => new(false, reason);
}
