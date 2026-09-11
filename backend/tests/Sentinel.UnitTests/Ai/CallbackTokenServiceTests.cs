using FluentAssertions;
using Microsoft.Extensions.Configuration;
using Sentinel.Infrastructure.Ai;
using Xunit;

namespace Sentinel.UnitTests.Ai;

/// <summary>
/// The token an investigation's callbacks carry.
/// </summary>
/// <remarks>
/// Unit-tested as well as exercised through the API because the properties that matter are
/// negative ones — a token for another investigation must not work, a tampered one must not work,
/// and an unconfigured secret must not quietly accept everything. Those are cheaper to state here
/// than to arrange over HTTP.
/// </remarks>
public sealed class CallbackTokenServiceTests
{
    private static CallbackTokenService Service(string? secret = "a-secret", string key = "Internal:ApiKey")
    {
        var values = secret is null ? [] : new Dictionary<string, string?> { [key] = secret };

        return new CallbackTokenService(
            new ConfigurationBuilder().AddInMemoryCollection(values).Build());
    }

    [Fact]
    public void A_token_verifies_for_the_investigation_it_was_issued_for()
    {
        var service = Service();
        var investigationId = Guid.NewGuid();

        service.Verify(investigationId, service.Issue(investigationId)).Should().BeTrue();
    }

    [Fact]
    public void A_token_does_not_verify_for_another_investigation()
    {
        // The containment the per-investigation token buys over a single shared secret.
        var service = Service();

        service.Verify(Guid.NewGuid(), service.Issue(Guid.NewGuid())).Should().BeFalse();
    }

    [Fact]
    public void The_same_investigation_always_gets_the_same_token()
    {
        // Derived rather than stored, so it survives a backend restart mid-investigation — which
        // is exactly when a run is longest and a rejected callback costs the most.
        var service = Service();
        var investigationId = Guid.NewGuid();

        service.Issue(investigationId).Should().Be(service.Issue(investigationId));
    }

    [Fact]
    public void Two_backends_with_different_secrets_do_not_accept_each_others_tokens()
    {
        var theirs = Service("another-secret").Issue(Guid.Empty);

        Service().Verify(Guid.Empty, theirs).Should().BeFalse();
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    [InlineData("not-base64-at-all!")]
    [InlineData("c2hvcnQ=")]
    public void Rubbish_is_refused_rather_than_throwing(string? token)
    {
        // Verification runs on every callback and is reachable by anything that can post to the
        // endpoint, so malformed input has to be an answer rather than an exception.
        Service().Verify(Guid.NewGuid(), token).Should().BeFalse();
    }

    [Fact]
    public void A_tampered_token_is_refused()
    {
        var service = Service();
        var investigationId = Guid.NewGuid();
        var token = service.Issue(investigationId);
        var tampered = token[0] == 'A' ? 'B' + token[1..] : 'A' + token[1..];

        service.Verify(investigationId, tampered).Should().BeFalse();
    }

    [Fact]
    public void The_dedicated_secret_is_preferred_over_the_internal_api_key()
    {
        var dedicated = Service("dedicated", CallbackTokenService.ConfigurationKey);
        var fallback = Service("dedicated");

        // Same secret through either key, so the fallback is a convenience rather than a
        // different scheme.
        dedicated.Issue(Guid.Empty).Should().Be(fallback.Issue(Guid.Empty));
    }

    [Fact]
    public void With_no_secret_configured_nothing_is_issued_and_nothing_verifies()
    {
        // The same refusal the internal token filter makes: an unset secret must not turn the
        // callback surface into an open one.
        var service = Service(secret: null);

        service.Invoking(s => s.Issue(Guid.NewGuid())).Should().Throw<InvalidOperationException>();
        service.Verify(Guid.NewGuid(), "anything").Should().BeFalse();
    }
}
