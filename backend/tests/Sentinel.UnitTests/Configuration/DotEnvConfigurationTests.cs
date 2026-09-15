using FluentAssertions;
using Sentinel.Api;
using Xunit;

namespace Sentinel.UnitTests.Configuration;

/// <summary>
/// The <c>.env</c> reader, which is the kind of code that fails quietly.
/// </summary>
/// <remarks>
/// It exists because the backend used to ignore the file every other process reads, so moving
/// PostgreSQL off a taken 5432 — which `.env.example` tells you to do — moved the AI service and
/// Alembic and left the backend authenticating against whatever else was listening there. The
/// error it produced named a password, not a port.
///
/// Worth testing rather than trusting because a configuration mistake does not throw: it produces
/// a process that starts and then talks to the wrong database.
/// </remarks>
public sealed class DotEnvConfigurationTests
{
    [Fact]
    public void Assembles_the_connection_string_from_the_postgres_variables()
    {
        var values = DotEnvConfiguration.ReadConfiguration([
            "POSTGRES_DB=sentinel",
            "POSTGRES_USER=sentinel",
            "POSTGRES_PASSWORD=sentinel_dev_pw",
            "POSTGRES_PORT=55432",
        ]);

        values["ConnectionStrings:Postgres"].Should().Be(
            "Host=localhost;Port=55432;Database=sentinel;Username=sentinel;Password=sentinel_dev_pw");
    }

    [Fact]
    public void Leaves_an_explicit_connection_string_alone()
    {
        // Compose passes exactly this, and it is more specific than the parts.
        var values = DotEnvConfiguration.ReadConfiguration([
            "ConnectionStrings__Postgres=Host=postgres;Port=5432;Database=sentinel;Username=sentinel;Password=x",
            "POSTGRES_DB=sentinel",
            "POSTGRES_USER=sentinel",
            "POSTGRES_PASSWORD=sentinel_dev_pw",
            "POSTGRES_PORT=55432",
        ]);

        values["ConnectionStrings:Postgres"].Should().Contain("Host=postgres");
        values["ConnectionStrings:Postgres"].Should().NotContain("55432");
    }

    [Fact]
    public void Builds_no_connection_string_when_the_parts_are_not_all_there()
    {
        // A .env that configures something else must not replace a connection string somebody
        // set deliberately in appsettings.
        var values = DotEnvConfiguration.ReadConfiguration(["GRAFANA_PORT=3001"]);

        values.Should().NotContainKey("ConnectionStrings:Postgres");
    }

    [Fact]
    public void Translates_double_underscores_to_the_configuration_separator()
    {
        var values = DotEnvConfiguration.ReadConfiguration(["Seed__AdminUsername=admin"]);

        values.Should().ContainKey("Seed:AdminUsername");
    }

    [Theory]
    [InlineData("# POSTGRES_PORT=9999", "a commented line is not configuration")]
    [InlineData("", "a blank line is skipped")]
    [InlineData("NOT_AN_ASSIGNMENT", "a line with no '=' is skipped")]
    [InlineData("=orphan", "a line with no key is skipped")]
    public void Ignores_what_is_not_an_assignment(string line, string because)
    {
        var values = DotEnvConfiguration.ReadConfiguration([line]);

        values.Should().BeEmpty(because);
    }

    [Fact]
    public void Strips_quotes_that_compose_would_have_accepted()
    {
        var values = DotEnvConfiguration.ReadConfiguration(["INTERNAL_API_KEY=\"a quoted secret\""]);

        values["INTERNAL_API_KEY"].Should().Be("a quoted secret");
    }

    [Fact]
    public void A_real_environment_variable_outranks_the_file()
    {
        const string key = "SENTINEL_DOTENV_PRECEDENCE_TEST";
        Environment.SetEnvironmentVariable(key, "from the environment");

        try
        {
            var values = DotEnvConfiguration.ReadConfiguration([$"{key}=from the file"]);

            values.Should().NotContainKey(key, "an exported variable overrides .env for one run");
        }
        finally
        {
            Environment.SetEnvironmentVariable(key, null);
        }
    }
}
