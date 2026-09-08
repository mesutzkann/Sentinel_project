namespace Sentinel.Domain.Entities;

/// <summary>
/// A platform user. Local accounts only — the requirement is a simple local setup, so there is
/// no external identity provider and no self-service signup; an admin is seeded at startup.
/// </summary>
public sealed class User
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public required string Username { get; set; }

    /// <summary>BCrypt hash. The plaintext password never leaves the request that set it.</summary>
    public required string PasswordHash { get; set; }

    public UserRole Role { get; set; } = UserRole.Viewer;

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
}
