using System.Collections.Concurrent;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Sentinel.Application.Common;
using Testcontainers.PostgreSql;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// The API running against a real PostgreSQL, started for the test run and thrown away after.
/// </summary>
/// <remarks>
/// A real database rather than an in-memory provider, because the things worth testing here only
/// exist in a real one. The incident code comes from a sequence and a database function; the
/// migration includes a filtered index whose predicate PostgreSQL parses. Both of those shipped
/// broken in Phase 1 and neither would have failed against an in-memory provider — the first
/// bug wrote an empty string where a generated value belonged, and the second aborted the whole
/// migration on a column name.
///
/// The same image and the same init scripts as compose, so the schemas, the sequence and
/// <c>next_incident_code()</c> exist exactly as they do in development.
/// </remarks>
public sealed class SentinelApiFactory : WebApplicationFactory<Program>, IAsyncLifetime
{
    public const string InternalApiKey = "integration-test-internal-token";
    public const string AdminUsername = "admin";
    public const string AdminPassword = "Admin123!";

    private readonly PostgreSqlContainer _postgres = new PostgreSqlBuilder()
        .WithImage("pgvector/pgvector:pg17")
        .WithDatabase("sentinel")
        .WithUsername("sentinel")
        .WithPassword("sentinel_test_pw")
        // pg_stat_statements is preloaded in compose for database-mcp. It is not needed by the
        // backend, so it is left out here to keep container start fast.
        .WithBindMount(InitScriptDirectory(), "/docker-entrypoint-initdb.d")
        .Build();

    /// <summary>Serializer matching the API's snake_case wire contract.</summary>
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    /// <summary>
    /// Settings applied as environment variables, which is the only layer that reliably wins.
    /// </summary>
    /// <remarks>
    /// Not <c>ConfigureAppConfiguration</c>, which is the obvious choice and does not work here.
    /// Under the minimal hosting model those callbacks run before <c>Program.cs</c> calls
    /// <c>WebApplication.CreateBuilder</c>, which then layers appsettings.json and
    /// appsettings.Development.json on top — so the development connection string won and every
    /// test connected to whatever PostgreSQL happened to be on localhost:5432 rather than to the
    /// container. The symptom was 28 identical authentication failures.
    ///
    /// CreateBuilder adds environment variables after the JSON files, so these win.
    /// </remarks>
    private static readonly string[] OverriddenKeys =
    [
        "ConnectionStrings__Postgres",
        "Jwt__SigningKey",
        "Seed__AdminUsername",
        "Seed__AdminPassword",
        "Internal__ApiKey",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ];

    public async Task InitializeAsync()
    {
        await _postgres.StartAsync();

        Environment.SetEnvironmentVariable(
            "ConnectionStrings__Postgres", _postgres.GetConnectionString());
        Environment.SetEnvironmentVariable(
            "Jwt__SigningKey", "integration-test-signing-key-at-least-32-characters");
        Environment.SetEnvironmentVariable("Seed__AdminUsername", AdminUsername);
        Environment.SetEnvironmentVariable("Seed__AdminPassword", AdminPassword);
        Environment.SetEnvironmentVariable("Internal__ApiKey", InternalApiKey);

        // Empty disables the OTLP exporter. Left set, every test run would spend its time
        // retrying against a collector that is not part of the test.
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "");
    }

    public new async Task DisposeAsync()
    {
        foreach (var key in OverriddenKeys)
        {
            Environment.SetEnvironmentVariable(key, null);
        }

        await _postgres.DisposeAsync();
        await base.DisposeAsync();
    }

    /// <summary>
    /// The AI service, replaced by a recorder.
    /// </summary>
    /// <remarks>
    /// What these tests are about is the backend's half of ADR-0002: that a run is registered,
    /// that the callback surface writes the rows, and that an unreachable agent leaves something
    /// a person can read. None of that should need a Python process and a local model, and a
    /// test that started one would be measuring the model's mood.
    /// </remarks>
    public RecordingAiServiceClient AiService { get; } = new();

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        // Development, because that is where the API turns on Swagger and the developer
        // exception page — and where a failure in these tests reads like a failure a developer
        // would see rather than a stripped production error.
        builder.UseEnvironment(Environments.Development);

        builder.ConfigureTestServices(services =>
        {
            services.RemoveAll<IAiServiceClient>();
            services.AddSingleton<IAiServiceClient>(AiService);
        });
    }

    /// <summary>
    /// The callback token for one investigation, computed the way the backend computes it.
    /// </summary>
    /// <remarks>
    /// Recomputed rather than read back from the service under test, so that a change to the
    /// scheme fails a test instead of passing one that agrees with itself. <c>Internal:ApiKey</c>
    /// is the key because no <c>Internal:CallbackSecret</c> is configured here, which is also the
    /// local-stack default.
    /// </remarks>
    public static string CallbackToken(Guid investigationId) => Convert.ToBase64String(
        HMACSHA256.HashData(
            Encoding.UTF8.GetBytes(InternalApiKey),
            Encoding.UTF8.GetBytes(investigationId.ToString("D"))));

    /// <summary>A client carrying one investigation's callback token, as the AI service would.</summary>
    public HttpClient CreateCallbackClient(Guid investigationId)
    {
        var client = CreateClient();
        client.DefaultRequestHeaders.Add("X-Callback-Token", CallbackToken(investigationId));
        return client;
    }

    /// <summary>An unauthenticated client.</summary>
    public HttpClient CreateAnonymousClient() => CreateClient();

    /// <summary>A client carrying the seeded admin's bearer token.</summary>
    public async Task<HttpClient> CreateAdminClientAsync()
    {
        var client = CreateClient();

        var response = await client.PostAsJsonAsync(
            "/api/auth/login",
            new { username = AdminUsername, password = AdminPassword },
            Json);

        response.EnsureSuccessStatusCode();

        var login = await response.Content.ReadFromJsonAsync<LoginResponse>(Json);

        client.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", login!.AccessToken);

        return client;
    }

    /// <summary>A client carrying the internal shared secret instead of a user token.</summary>
    public HttpClient CreateInternalClient()
    {
        var client = CreateClient();
        client.DefaultRequestHeaders.Add("X-Internal-Token", InternalApiKey);
        return client;
    }

    /// <summary>
    /// Locates <c>infrastructure/postgres/init</c> by walking up from the test binary.
    /// </summary>
    /// <remarks>
    /// Walked rather than hard-coded as <c>../../../../..</c>: that depends on the build
    /// configuration and target framework in the output path, and breaks silently when either
    /// changes — as a missing schema rather than as a missing directory.
    /// </remarks>
    private static string InitScriptDirectory()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);

        while (directory is not null)
        {
            var candidate = Path.Combine(directory.FullName, "infrastructure", "postgres", "init");

            if (Directory.Exists(candidate))
            {
                return candidate;
            }

            directory = directory.Parent;
        }

        throw new DirectoryNotFoundException(
            "Could not find infrastructure/postgres/init above " + AppContext.BaseDirectory);
    }

    public sealed record LoginResponse(string AccessToken, string Username, string Role);
}

/// <summary>
/// Stands in for the Python agent: records what it was asked, and can refuse.
/// </summary>
public sealed class RecordingAiServiceClient : IAiServiceClient
{
    public ConcurrentBag<StartInvestigationRequest> Requests { get; } = [];

    /// <summary>When set, every start fails with this message, as an unreachable service would.</summary>
    public string? FailWith { get; set; }

    public Task StartInvestigationAsync(
        StartInvestigationRequest request,
        CancellationToken cancellationToken = default)
    {
        if (FailWith is { } reason)
        {
            throw new AiServiceUnavailableException(reason);
        }

        Requests.Add(request);

        return Task.CompletedTask;
    }
}

/// <summary>
/// Shares one API and one database container across every test class.
/// </summary>
/// <remarks>
/// Starting a container per class would add roughly ten seconds each. The tests are written not
/// to depend on each other's data: each creates what it needs and asserts on that, rather than
/// on the total count of anything.
/// </remarks>
[CollectionDefinition(Name)]
public sealed class ApiCollection : ICollectionFixture<SentinelApiFactory>
{
    public const string Name = "sentinel-api";
}
