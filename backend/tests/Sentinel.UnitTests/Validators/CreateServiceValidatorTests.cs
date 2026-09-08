using FluentAssertions;
using Sentinel.Application.Features.Services;
using Xunit;

namespace Sentinel.UnitTests.Validators;

/// <summary>
/// The service name rule is worth testing on its own: it is the string that joins logs, metrics
/// and traces, and an accepted name that OpenTelemetry would not produce silently breaks every
/// correlation the agent depends on.
/// </summary>
public sealed class CreateServiceValidatorTests
{
    private readonly CreateServiceValidator _validator = new();

    [Theory]
    [InlineData("orders")]
    [InlineData("payment-service")]
    [InlineData("svc-2")]
    public void Accepts_names_matching_the_otel_convention(string name)
    {
        var result = _validator.Validate(Command(name));

        result.IsValid.Should().BeTrue();
    }

    [Theory]
    [InlineData("Orders", "uppercase is rejected")]
    [InlineData("payment_service", "underscores are rejected")]
    [InlineData("-orders", "a leading hyphen is rejected")]
    [InlineData("payment service", "spaces are rejected")]
    [InlineData("", "an empty name is rejected")]
    public void Rejects_names_that_cannot_be_a_service_name(string name, string because)
    {
        var result = _validator.Validate(Command(name));

        result.IsValid.Should().BeFalse(because);
    }

    [Fact]
    public void Rejects_a_display_name_longer_than_the_column()
    {
        var result = _validator.Validate(Command("orders") with { DisplayName = new string('x', 201) });

        result.IsValid.Should().BeFalse("the display_name column is varchar(200)");
    }

    private static CreateServiceCommand Command(string name) =>
        new(name, "Orders Service", null, null, null);
}
