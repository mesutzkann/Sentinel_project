using Sentinel.Samples.Common;

namespace Sentinel.Samples.Orders;

/// <summary>
/// Calls the payments service to authorise an order.
/// </summary>
/// <remarks>
/// The failure handling here is deliberately plain: exceptions surface as a failed order rather
/// than being swallowed, because several chaos scenarios depend on the caller genuinely failing
/// (8, 11, 13). A retry or fallback added here would hide exactly the signals the agent needs.
/// </remarks>
public sealed class PaymentsClient
{
    private readonly HttpClient _http;
    private readonly ILogger<PaymentsClient> _logger;

    public PaymentsClient(HttpClient http, ILogger<PaymentsClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    public async Task<AuthorizePaymentResponse?> AuthorizeAsync(
        Guid orderId,
        decimal amount,
        string currency,
        CancellationToken cancellationToken)
    {
        var response = await _http.PostAsJsonAsync(
            "/payments/authorize",
            new { OrderId = orderId, Amount = amount, Currency = currency },
            SampleJson.Options,
            cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            _logger.LogError(
                "Payment authorisation for order {OrderId} failed with {StatusCode}",
                orderId,
                (int)response.StatusCode);
            return null;
        }

        return await response.Content.ReadFromJsonAsync<AuthorizePaymentResponse>(
            SampleJson.Options, cancellationToken);
    }
}

public sealed record AuthorizePaymentResponse(Guid Id, string Status, string ProviderReference);
