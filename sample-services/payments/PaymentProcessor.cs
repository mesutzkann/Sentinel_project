using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Payments.Domain;

namespace Sentinel.Samples.Payments;

/// <summary>
/// Simulated payment authorisation. No external provider is called; the outcome is derived from
/// the amount so that behaviour is reproducible across evaluation runs.
/// </summary>
/// <remarks>
/// This file is named in the stack trace produced by chaos scenario 5
/// (NULL_REFERENCE_EXCEPTION), and source-code-mcp reads it during that investigation. Keep the
/// currency lookup and the authorisation path easy to follow.
/// </remarks>
public sealed class PaymentProcessor
{
    /// <summary>
    /// Minor-unit exponents per ISO 4217. Scenario 5 works by asking for a currency that is not
    /// in this map; the healthy path guards the lookup, the broken one does not.
    /// </summary>
    private static readonly IReadOnlyDictionary<string, CurrencyInfo> Currencies =
        new Dictionary<string, CurrencyInfo>(StringComparer.OrdinalIgnoreCase)
        {
            ["TRY"] = new("TRY", 2, 1.0m),
            ["USD"] = new("USD", 2, 34.0m),
            ["EUR"] = new("EUR", 2, 37.0m),
            ["GBP"] = new("GBP", 2, 43.0m),
        };

    private readonly ChaosRegistry _chaos;
    private readonly ILogger<PaymentProcessor> _logger;

    public PaymentProcessor(ChaosRegistry chaos, ILogger<PaymentProcessor> logger)
    {
        _chaos = chaos;
        _logger = logger;
    }

    public CurrencyInfo? FindCurrency(string code) =>
        Currencies.TryGetValue(code, out var info) ? info : null;

    public Payment Authorize(Guid orderId, decimal amount, string currencyCode)
    {
        var currency = FindCurrency(currencyCode);

        // The guard scenario 5 removes. Without it, the dereference below throws
        // NullReferenceException for any currency outside the map — a fast failure on the
        // subset of requests using an unmapped currency, with latency unaffected. The stack
        // trace names this file and line, which is what source-code-mcp reads during the
        // investigation.
        if (currency is null && !_chaos.IsEnabled(ChaosCodes.NullReferenceException))
        {
            _logger.LogWarning(
                "Order {OrderId} uses unsupported currency {Currency}", orderId, currencyCode);

            return new Payment
            {
                OrderId = orderId,
                Amount = amount,
                Currency = currencyCode,
                Status = PaymentStatus.Declined,
                ProviderReference = NewReference(),
            };
        }

        var amountInTry = Math.Round(amount * currency!.TryRate, currency.MinorUnits);

        // Amounts above the ceiling are declined. Deterministic, so an evaluation run that
        // expects a decline gets one every time.
        var status = amountInTry <= 50_000m ? PaymentStatus.Authorized : PaymentStatus.Declined;

        return new Payment
        {
            OrderId = orderId,
            Amount = amount,
            Currency = currency.Code,
            Status = status,
            ProviderReference = NewReference(),
        };
    }

    private static string NewReference() => $"PAY-{Guid.NewGuid():N}"[..20].ToUpperInvariant();
}

/// <param name="Code">ISO 4217 code.</param>
/// <param name="MinorUnits">Decimal places used when rounding.</param>
/// <param name="TryRate">Conversion rate to TRY. Fixed, because a live rate would make runs non-reproducible.</param>
public sealed record CurrencyInfo(string Code, int MinorUnits, decimal TryRate);
