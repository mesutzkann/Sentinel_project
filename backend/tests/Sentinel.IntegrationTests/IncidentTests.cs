using System.Net;
using System.Net.Http.Json;
using FluentAssertions;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// Incident creation, and the generated code that broke it.
/// </summary>
[Collection(ApiCollection.Name)]
public sealed class IncidentTests
{
    private readonly SentinelApiFactory _factory;

    public IncidentTests(SentinelApiFactory factory) => _factory = factory;

    [Fact]
    public async Task Creating_an_incident_returns_a_generated_code()
    {
        var client = await _factory.CreateAdminClientAsync();
        var serviceId = await FirstServiceIdAsync(client);

        var created = await CreateIncidentAsync(client, serviceId, "Payments returning 500s");

        created.IncidentCode.Should().MatchRegex(
            @"^INC-\d{5}$",
            "the code comes from sentinel.next_incident_code() and is the human-facing identifier");
    }

    /// <summary>
    /// The regression from Phase 1.
    /// </summary>
    /// <remarks>
    /// <c>IncidentCode</c> defaulted to <c>string.Empty</c>. EF omits a store-generated column
    /// from the INSERT only while the property still holds the CLR default, so it wrote '' and
    /// the database default never ran. The first incident got an empty code and the second
    /// collided with it on the unique index and returned a 500.
    ///
    /// Three incidents rather than two, so the test fails on a wrong sequence as well as on a
    /// collision.
    /// </remarks>
    [Fact]
    public async Task Incident_codes_are_unique_and_sequential_across_several_incidents()
    {
        var client = await _factory.CreateAdminClientAsync();
        var serviceId = await FirstServiceIdAsync(client);

        var codes = new List<string>();

        for (var i = 1; i <= 3; i++)
        {
            var created = await CreateIncidentAsync(client, serviceId, $"Sequential incident {i}");
            codes.Add(created.IncidentCode);
        }

        codes.Should().OnlyHaveUniqueItems();
        codes.Should().AllSatisfy(code => code.Should().MatchRegex(@"^INC-\d{5}$"));

        var numbers = codes.Select(c => int.Parse(c[4..])).ToList();
        numbers.Should().BeInAscendingOrder("the sequence hands out increasing values");
    }

    [Fact]
    public async Task A_created_incident_appears_in_the_list()
    {
        var client = await _factory.CreateAdminClientAsync();
        var serviceId = await FirstServiceIdAsync(client);

        var created = await CreateIncidentAsync(client, serviceId, "Listed incident");

        var incidents = await client.GetFromJsonAsync<List<IncidentResponse>>(
            "/api/incidents", SentinelApiFactory.Json);

        incidents.Should().Contain(i => i.Id == created.Id);
    }

    [Fact]
    public async Task A_new_incident_starts_open_and_names_its_service_and_creator()
    {
        var client = await _factory.CreateAdminClientAsync();
        var serviceId = await FirstServiceIdAsync(client);

        var created = await CreateIncidentAsync(client, serviceId, "Freshly opened");

        created.Status.Should().Be("open");
        created.ServiceName.Should().NotBeNullOrEmpty();
        created.CreatedByUsername.Should().Be(SentinelApiFactory.AdminUsername);
        created.InvestigationCount.Should().Be(0);
    }

    [Fact]
    public async Task An_incident_against_an_unknown_service_is_refused()
    {
        var client = await _factory.CreateAdminClientAsync();

        var response = await client.PostAsJsonAsync(
            "/api/incidents",
            new
            {
                service_id = Guid.NewGuid(),
                title = "Against a service that does not exist",
                severity = "high",
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().BeOneOf(
            HttpStatusCode.NotFound,
            HttpStatusCode.BadRequest,
            HttpStatusCode.UnprocessableEntity);
    }

    [Fact]
    public async Task An_incident_without_a_title_is_refused()
    {
        var client = await _factory.CreateAdminClientAsync();
        var serviceId = await FirstServiceIdAsync(client);

        var response = await client.PostAsJsonAsync(
            "/api/incidents",
            new { service_id = serviceId, title = "", severity = "high" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().BeOneOf(
            HttpStatusCode.BadRequest, HttpStatusCode.UnprocessableEntity);
    }

    [Fact]
    public async Task Creating_an_incident_requires_authentication()
    {
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/api/incidents",
            new { service_id = Guid.NewGuid(), title = "Anonymous", severity = "low" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    // ------------------------------------------------------------------------ helpers ----

    private static async Task<Guid> FirstServiceIdAsync(HttpClient client)
    {
        var services = await client.GetFromJsonAsync<List<ServiceResponse>>(
            "/api/services", SentinelApiFactory.Json);

        services.Should().NotBeNullOrEmpty("the five sample services are seeded at startup");

        return services![0].Id;
    }

    private static async Task<IncidentResponse> CreateIncidentAsync(
        HttpClient client,
        Guid serviceId,
        string title)
    {
        var response = await client.PostAsJsonAsync(
            "/api/incidents",
            new
            {
                service_id = serviceId,
                title,
                description = "Created by an integration test.",
                severity = "high",
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(
            HttpStatusCode.Created,
            "creating an incident should succeed: {0}",
            await response.Content.ReadAsStringAsync());

        return (await response.Content.ReadFromJsonAsync<IncidentResponse>(
            SentinelApiFactory.Json))!;
    }

    private sealed record ServiceResponse(Guid Id, string Name, string DisplayName);

    private sealed record IncidentResponse(
        Guid Id,
        string IncidentCode,
        Guid ServiceId,
        string ServiceName,
        string Title,
        string Severity,
        string Status,
        string? CreatedByUsername,
        int InvestigationCount);
}
