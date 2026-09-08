namespace Sentinel.Samples.Payments.Domain;

/// <summary>
/// A merchant's settlement balance, updated inside the authorisation transaction.
/// </summary>
/// <remarks>
/// Exists to give chaos scenario 3 (DB_DEADLOCK) two rows to contend over. A deadlock needs two
/// transactions taking the same two locks in opposite order, which means the service needs at
/// least two updatable rows in one transaction — a payment row and a balance row.
/// </remarks>
public sealed class MerchantBalance
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public required string MerchantCode { get; set; }

    public decimal Balance { get; set; }

    public DateTimeOffset UpdatedAt { get; set; } = DateTimeOffset.UtcNow;
}
