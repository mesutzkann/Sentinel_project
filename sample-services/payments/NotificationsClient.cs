using Sentinel.Samples.Payments.Domain;

namespace Sentinel.Samples.Payments;

/// <summary>
/// Sends the payment confirmation. Failures are logged and swallowed: a notification that does
/// not arrive must not undo a payment that did.
/// </summary>
/// <remarks>
/// This is the one place in the sample services where swallowing an error is correct, and it is
/// also what makes chaos scenarios 9 and 12 interesting — notifications fails completely while
/// orders and payments stay green, so the agent has to notice a service that is broken without
/// anyone complaining.
/// </remarks>
public sealed class NotificationsClient
{
    private readonly HttpClient _http;
    private readonly ILogger<NotificationsClient> _logger;

    public NotificationsClient(HttpClient http, ILogger<NotificationsClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    public async Task SendPaymentConfirmationAsync(Payment payment, CancellationToken cancellationToken)
    {
        try
        {
            var response = await _http.PostAsJsonAsync(
                "/notifications",
                new
                {
                    recipient = $"order-{payment.OrderId}@example.com",
                    channel = "email",
                    subject = "Payment confirmed",
                    body = $"Payment {payment.ProviderReference} for {payment.Amount} "
                           + $"{payment.Currency} was authorised.",
                },
                cancellationToken);

            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning(
                    "Notification for payment {PaymentId} rejected with {StatusCode}",
                    payment.Id,
                    (int)response.StatusCode);
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(
                ex,
                "Notification for payment {PaymentId} could not be delivered",
                payment.Id);
        }
    }
}
