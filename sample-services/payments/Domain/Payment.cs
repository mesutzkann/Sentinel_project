namespace Sentinel.Samples.Payments.Domain;

public enum PaymentStatus
{
    Authorized,
    Declined,
    Failed,
}

public sealed class Payment
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid OrderId { get; set; }

    public decimal Amount { get; set; }

    public required string Currency { get; set; }

    public PaymentStatus Status { get; set; }

    /// <summary>Reference the (simulated) payment provider returns, echoed back to orders.</summary>
    public required string ProviderReference { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}
