using FluentAssertions;
using Sentinel.Application.Features.Incidents;
using Sentinel.Domain;
using Xunit;

namespace Sentinel.UnitTests.Validators;

public sealed class CreateIncidentValidatorTests
{
    private readonly CreateIncidentValidator _validator = new();

    [Fact]
    public void Accepts_an_incident_with_no_explicit_start_time()
    {
        var result = _validator.Validate(Command() with { StartedAt = null });

        result.IsValid.Should().BeTrue("StartedAt defaults to now when it is omitted");
    }

    [Fact]
    public void Accepts_a_start_time_in_the_past()
    {
        var result = _validator.Validate(Command() with { StartedAt = DateTimeOffset.UtcNow.AddHours(-3) });

        result.IsValid.Should().BeTrue("incidents are usually reported after they begin");
    }

    [Fact]
    public void Rejects_a_start_time_in_the_future()
    {
        var result = _validator.Validate(Command() with { StartedAt = DateTimeOffset.UtcNow.AddHours(1) });

        result.IsValid.Should().BeFalse(
            "deployment correlation compares incident onset against commit timestamps, "
            + "and a future onset would match nothing");
    }

    [Fact]
    public void Allows_a_small_clock_skew()
    {
        var result = _validator.Validate(Command() with { StartedAt = DateTimeOffset.UtcNow.AddSeconds(30) });

        result.IsValid.Should().BeTrue(
            "a caller's clock can be slightly ahead, and rejecting that would be surprising");
    }

    [Fact]
    public void Requires_a_service()
    {
        var result = _validator.Validate(Command() with { ServiceId = Guid.Empty });

        result.IsValid.Should().BeFalse();
    }

    private static CreateIncidentCommand Command() => new(
        Guid.NewGuid(),
        "Payment authorisation failing",
        null,
        IncidentSeverity.High,
        null);
}
