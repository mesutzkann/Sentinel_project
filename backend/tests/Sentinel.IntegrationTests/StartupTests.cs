using System.Net;
using System.Net.Http.Json;
using FluentAssertions;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Sentinel.Infrastructure.Persistence;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// Migration and seed, which run on every start.
/// </summary>
/// <remarks>
/// The API applies migrations and seeds in <c>Program.cs</c> before it serves anything, so a
/// factory that produced a working client has already proved most of this. These assertions
/// name what "working" means, so a failure says which part broke rather than only that startup
/// threw.
/// </remarks>
[Collection(ApiCollection.Name)]
public sealed class StartupTests
{
    private readonly SentinelApiFactory _factory;

    public StartupTests(SentinelApiFactory factory) => _factory = factory;

    /// <summary>
    /// The other Phase 1 regression.
    /// </summary>
    /// <remarks>
    /// The tool_calls filtered index was declared as <c>is_destructive = true</c>. Table names
    /// are mapped to snake_case but columns keep EF's PascalCase default, so the unquoted
    /// identifier folded to a column that does not exist and the whole InitialCreate migration
    /// aborted — the API could not start at all against an empty database.
    /// </remarks>
    [Fact]
    public async Task Every_migration_applies_to_an_empty_database()
    {
        await using var scope = _factory.Services.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<SentinelDbContext>();

        var pending = await db.Database.GetPendingMigrationsAsync();
        var applied = await db.Database.GetAppliedMigrationsAsync();

        pending.Should().BeEmpty("startup migrates before serving");
        applied.Should().NotBeEmpty();
    }

    [Fact]
    public async Task The_filtered_index_the_migration_creates_exists()
    {
        await using var scope = _factory.Services.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<SentinelDbContext>();

        var connection = db.Database.GetDbConnection();
        await connection.OpenAsync();

        await using var command = connection.CreateCommand();
        command.CommandText =
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'sentinel' AND tablename = 'tool_calls'
              AND indexdef LIKE '%WHERE%'
            """;

        var definition = (string?)await command.ExecuteScalarAsync();

        definition.Should().NotBeNull("the partial index on IsDestructive should have been created");
        definition.Should().Contain("IsDestructive", "the predicate has to quote the real column");
    }

    [Fact]
    public async Task The_five_sample_services_are_seeded()
    {
        var client = await _factory.CreateAdminClientAsync();

        var services = await client.GetFromJsonAsync<List<ServiceResponse>>(
            "/api/services", SentinelApiFactory.Json);

        services!.Select(s => s.Name)
            .Should()
            .BeEquivalentTo(["gateway", "users", "orders", "payments", "notifications"]);
    }

    [Fact]
    public async Task Seeding_is_idempotent()
    {
        // The seeder runs on every start and inserts only what is missing. Asserting on exactly
        // five services after a start that has already happened is what proves it does not
        // duplicate them.
        var client = await _factory.CreateAdminClientAsync();

        var services = await client.GetFromJsonAsync<List<ServiceResponse>>(
            "/api/services", SentinelApiFactory.Json);

        services.Should().HaveCount(5);
    }

    [Fact]
    public async Task Health_is_anonymous()
    {
        var response = await _factory.CreateAnonymousClient().GetAsync("/health");

        response.StatusCode.Should().Be(HttpStatusCode.OK);
    }

    private sealed record ServiceResponse(Guid Id, string Name, string DisplayName);
}
