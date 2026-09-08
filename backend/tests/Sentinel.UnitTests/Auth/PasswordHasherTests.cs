using FluentAssertions;
using Sentinel.Infrastructure.Auth;
using Xunit;

namespace Sentinel.UnitTests.Auth;

public sealed class PasswordHasherTests
{
    private readonly BCryptPasswordHasher _hasher = new();

    [Fact]
    public void Verifies_the_password_it_hashed()
    {
        var hash = _hasher.Hash("Admin123!");

        _hasher.Verify("Admin123!", hash).Should().BeTrue();
    }

    [Fact]
    public void Rejects_a_different_password()
    {
        var hash = _hasher.Hash("Admin123!");

        _hasher.Verify("admin123!", hash).Should().BeFalse("BCrypt comparison is case sensitive");
    }

    [Fact]
    public void Produces_a_different_hash_each_time()
    {
        var first = _hasher.Hash("Admin123!");
        var second = _hasher.Hash("Admin123!");

        first.Should().NotBe(second, "each hash carries its own salt");
        _hasher.Verify("Admin123!", first).Should().BeTrue();
        _hasher.Verify("Admin123!", second).Should().BeTrue();
    }

    [Fact]
    public void Treats_a_malformed_hash_as_a_failed_verification()
    {
        // A row edited by hand, or written by an older scheme. Login should fail rather than the
        // request throwing a 500 that hides the real cause.
        var act = () => _hasher.Verify("Admin123!", "not-a-bcrypt-hash");

        act.Should().NotThrow();
        _hasher.Verify("Admin123!", "not-a-bcrypt-hash").Should().BeFalse();
    }
}
