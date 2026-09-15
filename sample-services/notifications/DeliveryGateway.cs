using Sentinel.Samples.Common;
using Sentinel.Samples.Notifications.Domain;

namespace Sentinel.Samples.Notifications;

/// <summary>
/// The outbound leg to the third-party delivery provider (SMTP relay, SMS gateway, push service).
/// </summary>
/// <remarks>
/// A real HTTP call to a real process, and that is what chaos scenario 12
/// (EXTERNAL_DEPENDENCY_UNAVAILABLE) needs. The provider is not instrumented, so a failure here
/// terminates at this service's outgoing client span with nothing below it — the failing span is
/// outside the boundary, which is the scenario's discriminator and the thing that separates it
/// from every fault that lives in one of the five services.
///
/// The retries are the second half of the signature. Three attempts with a short backoff is
/// ordinary client behaviour against a 5xx, and it is what makes the log fill with retry lines
/// rather than a single failure.
/// </remarks>
public sealed class DeliveryGateway
{
    /// <summary>Attempts, not retries. A 503 is worth asking again about; a 400 is not.</summary>
    private const int MaxAttempts = 3;

    private readonly HttpClient _client;
    private readonly ILogger<DeliveryGateway> _logger;

    public DeliveryGateway(HttpClient client, ILogger<DeliveryGateway> logger)
    {
        _client = client;
        _logger = logger;
    }

    public async Task<DeliveryResult> DeliverAsync(
        Notification notification,
        CancellationToken cancellationToken)
    {
        var payload = new
        {
            recipient = notification.Recipient,
            subject = notification.Subject,
            body = notification.Body,
        };

        string? lastFailure = null;

        for (var attempt = 1; attempt <= MaxAttempts; attempt++)
        {
            try
            {
                var response = await _client.PostAsJsonAsync(
                    "/send", payload, SampleJson.Options, cancellationToken);

                if (response.IsSuccessStatusCode)
                {
                    _logger.LogInformation(
                        "Delivered {Channel} notification to {Recipient}",
                        notification.Channel,
                        notification.Recipient);

                    return DeliveryResult.Success;
                }

                lastFailure =
                    $"The delivery provider answered {(int)response.StatusCode} "
                    + $"{response.StatusCode}";

                // Only a 5xx is worth another attempt. Retrying a rejected payload would turn one
                // bad request into three and say nothing new.
                if ((int)response.StatusCode < 500)
                {
                    break;
                }
            }
            catch (HttpRequestException exception)
            {
                lastFailure = $"The delivery provider could not be reached: {exception.Message}";
            }

            if (attempt < MaxAttempts)
            {
                _logger.LogWarning(
                    "Delivery to {Recipient} failed on attempt {Attempt} of {MaxAttempts}: "
                    + "{Reason}. Retrying.",
                    notification.Recipient,
                    attempt,
                    MaxAttempts,
                    lastFailure);

                await Task.Delay(TimeSpan.FromMilliseconds(50 * attempt), cancellationToken);
            }
        }

        return DeliveryResult.Failed(
            lastFailure is null ? "The delivery provider did not accept it." : lastFailure + ".");
    }
}

public sealed record DeliveryResult(bool Succeeded, string? FailureReason)
{
    public static readonly DeliveryResult Success = new(true, null);

    public static DeliveryResult Failed(string reason) => new(false, reason);
}
