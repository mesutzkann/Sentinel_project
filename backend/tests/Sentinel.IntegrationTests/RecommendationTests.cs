using System.Net;
using System.Net.Http.Json;
using FluentAssertions;
using Sentinel.Application.Common;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// The human in the loop: what approving a fix actually does.
/// </summary>
/// <remarks>
/// The recommendation these tests approve is created the way a real one is — by the agent
/// posting a completed investigation through the callback surface — so the row under test is the
/// row the system produces rather than one a fixture invented.
/// </remarks>
[Collection(ApiCollection.Name)]
public sealed class RecommendationTests
{
    private readonly SentinelApiFactory _factory;

    public RecommendationTests(SentinelApiFactory factory) => _factory = factory;

    [Fact]
    public async Task Approving_runs_the_action_and_records_who_approved_it()
    {
        var client = await _factory.CreateAdminClientAsync();
        var recommendation = await PendingRecommendationAsync(client);

        var response = await client.PostAsJsonAsync(
            $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json);

        response.StatusCode.Should().Be(
            HttpStatusCode.OK, await response.Content.ReadAsStringAsync());

        var outcome = await Body<OutcomeResponse>(response);

        // Verified, not merely executed: the AI service measured the symptom and it improved.
        outcome.Status.Should().Be("verified");
        outcome.Confirmed.Should().BeTrue();
        outcome.Verdict.Should().Be("resolved");

        var asked = _factory.AiService.Executions.Single(e => e.RecommendationId == recommendation);
        asked.Tool.Should().Be("update_env_and_restart");
        asked.ApprovalToken.Should().NotBeNullOrWhiteSpace();

        // The service the fix is about travels with it, because that is what gets re-measured.
        asked.Service.Should().Be("orders");
    }

    [Fact]
    public async Task An_action_that_ran_and_changed_nothing_is_executed_rather_than_verified()
    {
        var client = await _factory.CreateAdminClientAsync();
        var recommendation = await PendingRecommendationAsync(client);

        _factory.AiService.ExecutionResult = new ExecuteActionResult(
            Executed: true,
            Confirmed: false,
            Verdict: "unchanged",
            Summary: "The error rate is 17.0% against 18.0% before: no measurable change.",
            Error: null,
            RawJson: """{"executed":true,"confirmed":false}""");

        try
        {
            var outcome = await Body<OutcomeResponse>(await client.PostAsJsonAsync(
                $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json));

            // The honest word for it: something was done and the incident is still there.
            outcome.Status.Should().Be("executed");
            outcome.Confirmed.Should().BeFalse();
        }
        finally
        {
            _factory.AiService.ExecutionResult = Default;
        }
    }

    [Fact]
    public async Task A_recommendation_can_only_be_approved_once()
    {
        var client = await _factory.CreateAdminClientAsync();
        var recommendation = await PendingRecommendationAsync(client);

        await client.PostAsJsonAsync(
            $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json);

        var again = await client.PostAsJsonAsync(
            $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json);

        // A second approval would mint a second token for a change that has already happened.
        again.StatusCode.Should().Be(HttpStatusCode.Conflict);
    }

    [Fact]
    public async Task Rejecting_keeps_the_reason_and_runs_nothing()
    {
        var client = await _factory.CreateAdminClientAsync();
        var recommendation = await PendingRecommendationAsync(client);

        var outcome = await Body<OutcomeResponse>(await client.PostAsJsonAsync(
            $"/api/recommendations/{recommendation}/reject",
            new { reason = "The pool size is deliberate; it is sized for the batch job." },
            SentinelApiFactory.Json));

        outcome.Status.Should().Be("rejected");
        outcome.Summary.Should().Contain("deliberate");
        _factory.AiService.Executions.Should().NotContain(e => e.RecommendationId == recommendation);
    }

    [Fact]
    public async Task An_unreachable_agent_leaves_the_recommendation_failed_rather_than_executing()
    {
        var client = await _factory.CreateAdminClientAsync();
        var recommendation = await PendingRecommendationAsync(client);

        _factory.AiService.FailWith = "The AI service did not answer.";

        try
        {
            var response = await client.PostAsJsonAsync(
                $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json);

            response.StatusCode.Should().Be(HttpStatusCode.BadGateway);
        }
        finally
        {
            _factory.AiService.FailWith = null;
        }

        // A row left Executing is one nobody can act on and that looks like work in progress for
        // ever. The approval itself is still recorded: a person did decide.
        var again = await client.PostAsJsonAsync(
            $"/api/recommendations/{recommendation}/approve", new { }, SentinelApiFactory.Json);

        again.StatusCode.Should().Be(HttpStatusCode.Conflict);
    }

    // ---------------------------------------------------------------------- helpers ----

    private static readonly ExecuteActionResult Default = new(
        Executed: true,
        Confirmed: true,
        Verdict: "resolved",
        Summary: "The error rate went from 18.0% to 0.2%: the symptom is gone.",
        Error: null,
        RawJson: """{"executed":true,"confirmed":true,"verification":{"verdict":"resolved"}}""");

    /// <summary>An incident, an investigation, and the recommendation the agent proposed for it.</summary>
    private async Task<Guid> PendingRecommendationAsync(HttpClient client)
    {
        var services = await client.GetFromJsonAsync<List<ServiceResponse>>(
            "/api/services", SentinelApiFactory.Json);
        var orders = services!.First(service => service.Name == "orders");

        var incident = await Body<IncidentResponse>(await client.PostAsJsonAsync(
            "/api/incidents",
            new
            {
                service_id = orders.Id,
                title = "Orders timing out",
                description = "Created by a recommendation test.",
                severity = "high",
            },
            SentinelApiFactory.Json));

        var investigation = await Body<InvestigationResponse>(await client.PostAsJsonAsync(
            $"/api/incidents/{incident.Id}/investigate", new { }, SentinelApiFactory.Json));

        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        await callbacks.PostAsJsonAsync(
            $"/internal/investigations/{investigation.Id}/events",
            Completed(investigation.Id),
            SentinelApiFactory.Json);

        var detail = await client.GetFromJsonAsync<InvestigationDetailResponse>(
            $"/api/investigations/{investigation.Id}", SentinelApiFactory.Json);

        return detail!.Recommendations.Single().Id;
    }

    /// <summary>The terminal event of a run, trimmed to what a recommendation needs.</summary>
    private static object Completed(Guid investigationId) => new
    {
        investigation_id = investigationId,
        sequence = 1,
        type = "completed",
        state = "COMPLETED",
        message = "investigation ended in COMPLETED",
        payload = new
        {
            final_state = "COMPLETED",
            result = new
            {
                root_cause = new
                {
                    title = "The connection pool was reduced to 20",
                    category = "DB_CONNECTION_POOL_EXHAUSTION",
                    explanation = "200 of 200 connections are in use after MaxPoolSize was lowered.",
                    confidence = 0.79,
                },
                recommendations = new object[]
                {
                    new
                    {
                        action_code = "RESTORE_POOL_SIZE",
                        description = "Restore MaxPoolSize to 200 and restart orders.",
                        tool_name = "update_env_and_restart",
                        tool_args = new { name = "sentinel-orders" },
                        requires_approval = true,
                    },
                },
            },
        },
        timestamp = DateTimeOffset.UtcNow,
    };

    private static async Task<T> Body<T>(HttpResponseMessage response) =>
        (await response.Content.ReadFromJsonAsync<T>(SentinelApiFactory.Json))!;

    private sealed record OutcomeResponse(
        Guid Id,
        string Status,
        bool Executed,
        bool Confirmed,
        string Verdict,
        string Summary,
        string? Error);

    private sealed record ServiceResponse(Guid Id, string Name);

    private sealed record IncidentResponse(Guid Id, string IncidentCode, string Status);

    private sealed record InvestigationResponse(Guid Id, string Status);

    private sealed record InvestigationDetailResponse(
        Guid Id,
        List<RecommendationResponse> Recommendations);

    private sealed record RecommendationResponse(Guid Id, string ActionCode, string Status);
}
