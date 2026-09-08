namespace Sentinel.Samples.Users.Domain;

/// <summary>A customer account. Root of the checkout flow: user -> order -> payment -> notification.</summary>
public sealed class User
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public required string Email { get; set; }

    public required string FullName { get; set; }

    public bool IsActive { get; set; } = true;

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}
