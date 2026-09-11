using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using FluentAssertions;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// The backend's half of ADR-0002: starting a run, and the callback surface that fills it in.
/// </summary>
/// <remarks>
/// The events posted here are the real ones. They were taken from a run of the agent against the
/// pool-exhaustion scenario, so a change to the AI service's envelope breaks these tests rather
/// than being discovered during a demo — which is the only reason to write the payloads out in
/// full rather than building them from a helper that agrees with whatever the backend expects.
/// </remarks>
[Collection(ApiCollection.Name)]
public sealed class InvestigationTests
{
    private readonly SentinelApiFactory _factory;

    public InvestigationTests(SentinelApiFactory factory) => _factory = factory;

    // ------------------------------------------------------------------- starting ----

    [Fact]
    public async Task Starting_an_investigation_hands_it_to_the_agent()
    {
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Orders timing out");

        var response = await client.PostAsJsonAsync(
            $"/api/incidents/{incident.Id}/investigate",
            new { query = "orders is timing out, find out why" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(
            HttpStatusCode.Accepted,
            "the run has started, not finished: {0}",
            await response.Content.ReadAsStringAsync());

        var investigation = await Body<InvestigationResponse>(response);
        investigation.Status.Should().Be("running");

        var asked = _factory.AiService.Requests.Single(r => r.InvestigationId == investigation.Id);
        asked.IncidentCode.Should().Be(incident.IncidentCode, "the agent names the incident a human recognises");
        asked.Query.Should().Be("orders is timing out, find out why");
        asked.CallbackUrl.Should().Be($"/internal/investigations/{investigation.Id}/events");
        asked.CallbackToken.Should().Be(SentinelApiFactory.CallbackToken(investigation.Id));

        var reloaded = await client.GetFromJsonAsync<IncidentResponse>(
            $"/api/incidents/{incident.Id}", SentinelApiFactory.Json);

        reloaded!.Status.Should().Be("investigating");
    }

    [Fact]
    public async Task A_second_investigation_of_the_same_incident_is_refused()
    {
        // Two agents on one incident would interleave two timelines on one screen and spend
        // twice the model time to disagree with themselves.
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Double investigated");

        await StartAsync(client, incident.Id);
        var second = await client.PostAsJsonAsync(
            $"/api/incidents/{incident.Id}/investigate", new { }, SentinelApiFactory.Json);

        second.StatusCode.Should().Be(HttpStatusCode.Conflict);
    }

    [Fact]
    public async Task An_unreachable_agent_leaves_a_failed_investigation_with_the_reason()
    {
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Agent is down");

        _factory.AiService.FailWith = "connection refused";

        try
        {
            var response = await client.PostAsJsonAsync(
                $"/api/incidents/{incident.Id}/investigate", new { }, SentinelApiFactory.Json);

            response.StatusCode.Should().Be(HttpStatusCode.BadGateway, "the dependency failed, not the request");
        }
        finally
        {
            _factory.AiService.FailWith = null;
        }

        var investigations = await client.GetFromJsonAsync<List<InvestigationResponse>>(
            $"/api/investigations?incidentId={incident.Id}", SentinelApiFactory.Json);

        // The row stays. Deleting it would leave whoever pressed Investigate with nothing to
        // look at and no reason why.
        var failed = investigations!.Single();
        failed.Status.Should().Be("failed");
        failed.FailureReason.Should().Contain("connection refused");

        var reloaded = await client.GetFromJsonAsync<IncidentResponse>(
            $"/api/incidents/{incident.Id}", SentinelApiFactory.Json);

        reloaded!.Status.Should().Be("open", "nothing is investigating it");
    }

    [Fact]
    public async Task Investigating_requires_an_engineer()
    {
        var response = await _factory.CreateAnonymousClient().PostAsJsonAsync(
            $"/api/incidents/{Guid.NewGuid()}/investigate", new { }, SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    // ------------------------------------------------------------------- the token ----

    [Fact]
    public async Task An_event_without_the_callback_token_is_refused()
    {
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Unguarded")).Id);

        var response = await _factory.CreateAnonymousClient().PostAsJsonAsync(
            EventsUrl(investigation.Id), Step(1, "PLAN", "planned"), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task One_investigations_token_does_not_open_another()
    {
        // The point of deriving the token per investigation rather than sharing one secret: a
        // token that leaks from one run cannot write events into another.
        var client = await _factory.CreateAdminClientAsync();
        var mine = await StartAsync(client, (await CreateIncidentAsync(client, "Mine")).Id);

        var response = await _factory.CreateCallbackClient(Guid.NewGuid()).PostAsJsonAsync(
            EventsUrl(mine.Id), Step(1, "PLAN", "planned"), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_user_token_does_not_open_the_callback_surface()
    {
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Not a browser")).Id);

        var response = await client.PostAsJsonAsync(
            EventsUrl(investigation.Id), Step(1, "PLAN", "planned"), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task An_event_for_an_investigation_that_does_not_exist_is_a_404()
    {
        var unknown = Guid.NewGuid();

        var response = await _factory.CreateCallbackClient(unknown).PostAsJsonAsync(
            EventsUrl(unknown), Step(1, "PLAN", "planned"), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.NotFound);
    }

    // ------------------------------------------------------------------- the timeline ----

    [Fact]
    public async Task A_step_becomes_a_timeline_entry_with_the_calls_behind_it()
    {
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Collecting")).Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        var response = await callbacks.PostAsJsonAsync(
            EventsUrl(investigation.Id),
            new
            {
                investigation_id = investigation.Id,
                sequence = 1,
                type = "step_completed",
                state = "COLLECT_DATABASE",
                message = "COLLECT_DATABASE: 3 fact(s) from 3 call(s)",
                payload = new { evidence_added = 3, budget_remaining = 16, duration_ms = 412 },
                tool_calls = new[]
                {
                    new
                    {
                        server = "database-mcp",
                        tool = "get_connection_count",
                        args = new { },
                        result_summary = "database connections: 20 of 20 used, 0 spare",
                        success = true,
                        latency_ms = 41,
                    },
                },
                timestamp = DateTimeOffset.UtcNow,
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.OK, await response.Content.ReadAsStringAsync());
        (await Body<AckResponse>(response)).Applied.Should().BeTrue();

        var detail = await DetailAsync(client, investigation.Id);

        var step = detail.Steps.Single();
        step.Sequence.Should().Be(1);
        step.State.Should().Be("COLLECT_DATABASE");
        step.DurationMs.Should().Be(412);
        step.Payload!.Value.GetProperty("evidence_added").GetInt32().Should().Be(3);

        var call = detail.ToolCalls.Single();
        call.Server.Should().Be("database-mcp");
        call.Tool.Should().Be("get_connection_count");
        call.StepId.Should().Be(step.Id, "a tool call belongs to the step that made it");
    }

    [Fact]
    public async Task A_model_call_reported_with_a_step_becomes_a_prediction_row()
    {
        // The agent's model usage lands in model_predictions without a second round trip: the
        // event already carries what the row needs.
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Reasoning")).Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        await callbacks.PostAsJsonAsync(
            EventsUrl(investigation.Id),
            new
            {
                sequence = 1,
                type = "step_completed",
                state = "VALIDATE",
                message = "the critic accepts the conclusion",
                payload = new { prompt_id = "critic.v1", valid = true },
                llm_usage = new
                {
                    model = "qwen2.5:3b-instruct",
                    prompt_tokens = 1100,
                    completion_tokens = 210,
                    latency_ms = 8400,
                    retries = 0,
                },
                timestamp = DateTimeOffset.UtcNow,
            },
            SentinelApiFactory.Json);

        var predictions = await client.GetFromJsonAsync<List<PredictionResponse>>(
            $"/api/model-predictions?investigationId={investigation.Id}", SentinelApiFactory.Json);

        var prediction = predictions!.Single();
        prediction.ModelName.Should().Be("qwen2.5:3b-instruct");
        prediction.PromptTokens.Should().Be(1100);
        prediction.Purpose.Should().Be("validation", "the critic is a different cost from reasoning");
    }

    [Fact]
    public async Task A_redelivered_step_is_not_recorded_twice()
    {
        // Delivery is at-least-once (ADR-0006): a retry means the agent never saw the answer,
        // not that anything happened twice.
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Retried")).Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);
        var url = EventsUrl(investigation.Id);

        var first = await callbacks.PostAsJsonAsync(url, Step(1, "PLAN", "planned"), SentinelApiFactory.Json);
        var again = await callbacks.PostAsJsonAsync(url, Step(1, "PLAN", "planned"), SentinelApiFactory.Json);

        (await Body<AckResponse>(first)).Applied.Should().BeTrue();

        again.StatusCode.Should().Be(HttpStatusCode.OK, "a 409 would make the agent retry a stored event");
        (await Body<AckResponse>(again)).Applied.Should().BeFalse();

        (await DetailAsync(client, investigation.Id)).Steps.Should().HaveCount(1);
    }

    [Fact]
    public async Task Findings_are_written_as_they_arrive()
    {
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Streaming")).Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);
        var url = EventsUrl(investigation.Id);

        await callbacks.PostAsJsonAsync(url, Step(1, "COLLECT_DATABASE", "3 facts"), SentinelApiFactory.Json);
        await callbacks.PostAsJsonAsync(
            url,
            new
            {
                sequence = 2,
                type = "evidence_found",
                state = "COLLECT_DATABASE",
                message = "database connections: 20 of 20 used, 0 spare",
                payload = new { index = 0, source = "database", weight = 0.9, tool = "database-mcp/get_connection_count" },
            },
            SentinelApiFactory.Json);
        await callbacks.PostAsJsonAsync(
            url,
            new
            {
                sequence = 3,
                type = "hypothesis",
                state = "RANK_HYPOTHESES",
                message = "1. The connection pool is exhausted (0.81)",
                payload = new { title = "The connection pool is exhausted", score = 0.81, rank = 1, selected = true },
            },
            SentinelApiFactory.Json);

        var detail = await DetailAsync(client, investigation.Id);

        var evidence = detail.Evidence.Single();
        evidence.Source.Should().Be("database");
        evidence.Weight.Should().Be(0.9m);
        evidence.StepId.Should().Be(
            detail.Steps.Single().Id,
            "the runner emits a state's step before the facts that state found");

        var hypothesis = detail.Hypotheses.Single();
        hypothesis.Rank.Should().Be(1);
        hypothesis.Score.Should().Be(0.81m);
        hypothesis.IsSelected.Should().BeTrue();
    }

    // ------------------------------------------------------------------- the end ----

    [Fact]
    public async Task The_final_payload_writes_what_the_lost_events_would_have()
    {
        // The promise ADR-0002 made when it accepted that delivery is not durable. Nothing but
        // the terminal event is posted here, and the investigation ends complete.
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Only the last event arrived");
        var investigation = await StartAsync(client, incident.Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        var response = await callbacks.PostAsJsonAsync(
            EventsUrl(investigation.Id),
            Completed(investigation.Id),
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.OK, await response.Content.ReadAsStringAsync());

        var detail = await DetailAsync(client, investigation.Id);

        detail.Investigation.Status.Should().Be("completed");
        detail.Investigation.RouterIntent.Should().Be("FULL_INVESTIGATION");
        detail.Investigation.LlmCalls.Should().Be(6);
        detail.Investigation.ToolCalls.Should().Be(10);
        detail.Investigation.TotalDurationMs.Should().Be(31653);

        detail.Evidence.Should().HaveCount(2);
        detail.Evidence[0].Raw!.Value.GetProperty("used").GetInt32().Should().Be(20);

        detail.Hypotheses.Should().HaveCount(2);
        detail.Hypotheses[0].Title.Should().Be("The connection pool is exhausted");

        detail.RootCause!.Category.Should().Be("DB_CONNECTION_POOL_EXHAUSTION");
        detail.RootCause.Confidence.Should().Be(0.79m);
        detail.RootCause.HypothesisId.Should().Be(
            detail.Hypotheses[0].Id,
            "the agent names the hypothesis it chose and the backend resolves it to a row");

        detail.Recommendations.Single().ActionCode.Should().Be("RESTORE_POOL_SIZE");
        detail.Recommendations.Single().RequiresApproval.Should().BeTrue();

        var reloaded = await client.GetFromJsonAsync<IncidentResponse>(
            $"/api/incidents/{incident.Id}", SentinelApiFactory.Json);

        reloaded!.Status.Should().Be("awaiting_approval", "there is something for a human to approve");
    }

    [Fact]
    public async Task Streamed_findings_are_completed_rather_than_duplicated()
    {
        var client = await _factory.CreateAdminClientAsync();
        var investigation = await StartAsync(client, (await CreateIncidentAsync(client, "Both halves")).Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);
        var url = EventsUrl(investigation.Id);

        await callbacks.PostAsJsonAsync(url, Step(1, "COLLECT_DATABASE", "3 facts"), SentinelApiFactory.Json);
        await callbacks.PostAsJsonAsync(
            url,
            new
            {
                sequence = 2,
                type = "evidence_found",
                state = "COLLECT_DATABASE",
                message = "database connections: 20 of 20 used, 0 spare",
                payload = new { index = 0, source = "database", weight = 0.9 },
            },
            SentinelApiFactory.Json);

        await callbacks.PostAsJsonAsync(url, Completed(investigation.Id), SentinelApiFactory.Json);

        var detail = await DetailAsync(client, investigation.Id);

        detail.Evidence.Should().HaveCount(2, "the streamed fact is matched, not inserted again");

        var streamed = detail.Evidence.Single(e => e.Summary.StartsWith("database connections"));
        streamed.Raw.Should().NotBeNull("the final payload carries the tool output the event did not");
        streamed.StepId.Should().NotBeNull("and it keeps the step that found it");
    }

    [Fact]
    public async Task A_failed_run_reopens_the_incident()
    {
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Agent died");
        var investigation = await StartAsync(client, incident.Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        await callbacks.PostAsJsonAsync(
            EventsUrl(investigation.Id),
            new
            {
                sequence = 1,
                type = "failed",
                state = "FAILED",
                message = "LlmUnavailableError: Ollama is not running",
                payload = new
                {
                    reached_from = "GENERATE_HYPOTHESES",
                    transitions = 9,
                    result = new
                    {
                        status = "failed",
                        final_state = "FAILED",
                        failure_reason = "LlmUnavailableError: Ollama is not running",
                        usage = new { llm_calls = 2, tool_calls = 10 },
                    },
                },
                timestamp = DateTimeOffset.UtcNow,
            },
            SentinelApiFactory.Json);

        var detail = await DetailAsync(client, investigation.Id);

        detail.Investigation.Status.Should().Be("failed");
        detail.Investigation.FailureReason.Should().Contain("Ollama is not running");
        detail.Investigation.CompletedAt.Should().NotBeNull();

        var reloaded = await client.GetFromJsonAsync<IncidentResponse>(
            $"/api/incidents/{incident.Id}", SentinelApiFactory.Json);

        reloaded!.Status.Should().Be("open");
    }

    [Fact]
    public async Task A_run_that_stopped_for_a_human_is_complete_and_says_why()
    {
        // NEEDS_HUMAN is not a failure and not an approval: the agent finished, and what it
        // concluded is not something it will act on.
        var client = await _factory.CreateAdminClientAsync();
        var incident = await CreateIncidentAsync(client, "Not confident enough");
        var investigation = await StartAsync(client, incident.Id);
        var callbacks = _factory.CreateCallbackClient(investigation.Id);

        await callbacks.PostAsJsonAsync(
            EventsUrl(investigation.Id),
            new
            {
                sequence = 1,
                type = "completed",
                state = "NEEDS_HUMAN",
                message = "the critic rejected the conclusion twice; stopping for a human",
                payload = new
                {
                    transitions = 12,
                    result = new
                    {
                        status = "completed",
                        final_state = "NEEDS_HUMAN",
                        needs_human = true,
                        failure_reason = "the critic rejected the conclusion twice",
                        root_cause = new
                        {
                            title = "A retry storm exhausted the pool",
                            category = "RETRY_STORM",
                            confidence = 0.62,
                            explanation = "The service retried failed operations.",
                        },
                        recommendations = Array.Empty<object>(),
                        usage = new { llm_calls = 6 },
                    },
                },
                timestamp = DateTimeOffset.UtcNow,
            },
            SentinelApiFactory.Json);

        var detail = await DetailAsync(client, investigation.Id);

        detail.Investigation.Status.Should().Be("completed");
        detail.Investigation.FailureReason.Should().Contain("rejected the conclusion twice");
        detail.RootCause!.Category.Should().Be("RETRY_STORM");
        detail.Recommendations.Should().BeEmpty();

        var reloaded = await client.GetFromJsonAsync<IncidentResponse>(
            $"/api/incidents/{incident.Id}", SentinelApiFactory.Json);

        reloaded!.Status.Should().Be("open", "there is nothing to approve");
    }

    [Fact]
    public async Task Reading_an_investigation_requires_authentication()
    {
        var response = await _factory.CreateAnonymousClient()
            .GetAsync($"/api/investigations/{Guid.NewGuid()}");

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    // ------------------------------------------------------------------------ helpers ----

    private static string EventsUrl(Guid investigationId) =>
        $"/internal/investigations/{investigationId}/events";

    private static object Step(int sequence, string state, string message) => new
    {
        sequence,
        type = "step_completed",
        state,
        message,
        payload = new { duration_ms = 10 },
        timestamp = DateTimeOffset.UtcNow,
    };

    /// <summary>A terminal event whose result is a whole investigation, as the agent sends it.</summary>
    private static object Completed(Guid investigationId) => new
    {
        investigation_id = investigationId,
        sequence = 99,
        type = "completed",
        state = "COMPLETED",
        message = "investigation ended in COMPLETED",
        payload = new
        {
            reached_from = "RECOMMEND_FIX",
            transitions = 14,
            duration_ms = 31653,
            result = new
            {
                investigation_id = investigationId,
                incident_code = "INC-00142",
                final_state = "COMPLETED",
                status = "completed",
                needs_human = false,
                transitions = 14,
                duration_ms = 31653,
                router_intent = "FULL_INVESTIGATION",
                router_output = new { intent = "FULL_INVESTIGATION", requires_mcp = true },
                target_service = "orders",
                evidence = new object[]
                {
                    new
                    {
                        index = 0,
                        source = "database",
                        summary = "database connections: 20 of 20 used, 0 spare",
                        weight = 0.9,
                        tool = "database-mcp/get_connection_count",
                        raw = new { used = 20, max_connections = 20, headroom = 0 },
                    },
                    new
                    {
                        index = 1,
                        source = "historical_incident",
                        summary = "postmortem INC-0023: MaxPoolSize was lowered from 200 to 20",
                        weight = 0.5,
                        tool = "rag/hybrid_rerank",
                        raw = new { external_id = "INC-0023" },
                    },
                },
                hypotheses = new object[]
                {
                    new
                    {
                        title = "The connection pool is exhausted",
                        description = "Every connection is in use and new work waits.",
                        score = 0.81,
                        rank = 1,
                        selected = true,
                    },
                    new
                    {
                        title = "A slow query is holding connections",
                        description = "No slow statements were found.",
                        score = 0.34,
                        rank = 2,
                        selected = false,
                    },
                },
                root_cause = new
                {
                    title = "The connection pool was reduced to 20",
                    category = "DB_CONNECTION_POOL_EXHAUSTION",
                    explanation = "200 of 200 connections are in use after MaxPoolSize was lowered.",
                    evidence = new[] { 0, 1 },
                    hypothesis = "The connection pool is exhausted",
                    confidence = 0.79,
                    confidence_breakdown = new { value = 0.79, threshold = 0.7 },
                    validator_confidence = 0.8,
                    validator_output = new { valid = true, concerns = Array.Empty<string>() },
                },
                recommendations = new object[]
                {
                    new
                    {
                        action_code = "RESTORE_POOL_SIZE",
                        description = "Restore MaxPoolSize to 200 and restart orders.",
                        tool_name = "update_env_and_restart",
                        tool_args = new { service = "orders" },
                        requires_approval = true,
                    },
                },
                notes = new[] { "git-mcp/get_recent_commits failed: git-mcp is not reachable" },
                usage = new
                {
                    llm_calls = 6,
                    prompt_tokens = 7279,
                    completion_tokens = 1417,
                    tool_calls = 10,
                    tool_budget = 25,
                    iterations = 0,
                },
            },
        },
        timestamp = DateTimeOffset.UtcNow,
    };

    private async Task<InvestigationResponse> StartAsync(HttpClient client, Guid incidentId)
    {
        var response = await client.PostAsJsonAsync(
            $"/api/incidents/{incidentId}/investigate", new { }, SentinelApiFactory.Json);

        response.StatusCode.Should().Be(
            HttpStatusCode.Accepted, await response.Content.ReadAsStringAsync());

        return await Body<InvestigationResponse>(response);
    }

    private static async Task<InvestigationDetailResponse> DetailAsync(HttpClient client, Guid id) =>
        (await client.GetFromJsonAsync<InvestigationDetailResponse>(
            $"/api/investigations/{id}", SentinelApiFactory.Json))!;

    private static async Task<T> Body<T>(HttpResponseMessage response) =>
        (await response.Content.ReadFromJsonAsync<T>(SentinelApiFactory.Json))!;

    private static async Task<IncidentResponse> CreateIncidentAsync(HttpClient client, string title)
    {
        var services = await client.GetFromJsonAsync<List<ServiceResponse>>(
            "/api/services", SentinelApiFactory.Json);

        var response = await client.PostAsJsonAsync(
            "/api/incidents",
            new
            {
                service_id = services![0].Id,
                title,
                description = "Created by an investigation test.",
                severity = "high",
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Created);

        return await Body<IncidentResponse>(response);
    }

    private sealed record ServiceResponse(Guid Id, string Name);

    private sealed record IncidentResponse(Guid Id, string IncidentCode, string Status);

    private sealed record InvestigationResponse(
        Guid Id,
        Guid IncidentId,
        string IncidentCode,
        string Query,
        string? RouterIntent,
        string Status,
        DateTimeOffset StartedAt,
        DateTimeOffset? CompletedAt,
        int? TotalDurationMs,
        int LlmCalls,
        int ToolCalls,
        string? FailureReason);

    private sealed record InvestigationDetailResponse(
        InvestigationResponse Investigation,
        List<StepResponse> Steps,
        List<EvidenceResponse> Evidence,
        List<HypothesisResponse> Hypotheses,
        RootCauseResponse? RootCause,
        List<RecommendationResponse> Recommendations,
        List<ToolCallResponse> ToolCalls);

    private sealed record StepResponse(
        Guid Id,
        int Sequence,
        string State,
        string Message,
        JsonElement? Payload,
        int? DurationMs);

    private sealed record EvidenceResponse(
        Guid Id,
        Guid? StepId,
        string Source,
        string Summary,
        decimal Weight,
        JsonElement? Raw);

    private sealed record HypothesisResponse(Guid Id, string Title, decimal Score, int Rank, bool IsSelected);

    private sealed record RootCauseResponse(
        Guid Id,
        Guid? HypothesisId,
        string Title,
        string Category,
        decimal Confidence,
        string? Explanation);

    private sealed record RecommendationResponse(
        Guid Id,
        string ActionCode,
        string Description,
        bool RequiresApproval,
        string Status);

    private sealed record ToolCallResponse(Guid Id, Guid? StepId, string Server, string Tool, bool Success);

    private sealed record PredictionResponse(
        Guid Id,
        Guid? InvestigationId,
        string ModelName,
        string Purpose,
        int PromptTokens);

    private sealed record AckResponse(Guid InvestigationId, int Sequence, bool Applied);
}
