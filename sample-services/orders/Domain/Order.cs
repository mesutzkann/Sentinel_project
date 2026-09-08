namespace Sentinel.Samples.Orders.Domain;

public enum OrderStatus
{
    Pending,
    Paid,
    PaymentFailed,
    Cancelled,
}

public sealed class Order
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid UserId { get; set; }

    public OrderStatus Status { get; set; } = OrderStatus.Pending;

    public decimal TotalAmount { get; set; }

    public string Currency { get; set; } = "TRY";

    /// <summary>Set once payments answers; null while the order is still pending.</summary>
    public string? PaymentReference { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;

    public List<OrderItem> Items { get; set; } = [];
}

public sealed class OrderItem
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public Guid OrderId { get; set; }

    public required string ProductName { get; set; }

    public int Quantity { get; set; }

    public decimal UnitPrice { get; set; }

    public decimal LineTotal => Quantity * UnitPrice;
}
