using System.Net;
using System.Net.Http.Json;
using FluentAssertions;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// The surface the AI service reports model calls through, and the secret that guards it.
/// </summary>
/// <remarks>
/// Worth integration-testing rather than unit-testing the filter: what matters is that the
/// header is required by the pipeline as wired, not that a class compares two strings. A filter
/// that is written correctly and never registered passes a unit test and leaves the endpoint
/// open.
/// </remarks>
[Collection(ApiCollection.Name)]
public sealed class InternalApiTests
{
    private readonly SentinelApiFactory _factory;

    public InternalApiTests(SentinelApiFactory factory) => _factory = factory;

    [Fact]
    public async Task A_prediction_is_recorded_with_the_internal_token()
    {
        var client = _factory.CreateInternalClient();

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions",
            Prediction(latencyMs: 1234),
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Created);

        var recorded = await response.Content.ReadFromJsonAsync<PredictionResponse>(
            SentinelApiFactory.Json);

        recorded!.LatencyMs.Should().Be(1234);
        recorded.PromptTokens.Should().Be(400);
        recorded.ValidJson.Should().BeTrue();
    }

    [Fact]
    public async Task A_call_without_the_token_is_refused()
    {
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions", Prediction(), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_call_with_the_wrong_token_is_refused()
    {
        var client = _factory.CreateAnonymousClient();
        client.DefaultRequestHeaders.Add("X-Internal-Token", "not-the-secret");

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions", Prediction(), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_user_token_does_not_open_the_internal_surface()
    {
        // The two credentials are for different callers and must not substitute for each other:
        // an admin's session should not let a browser post internal telemetry.
        var client = await _factory.CreateAdminClientAsync();

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions", Prediction(), SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_failed_generation_is_recorded_too()
    {
        // The row a structured-output success rate is measured from. Discarding it would leave
        // the dashboard able to report only the calls that worked.
        var client = _factory.CreateInternalClient();

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions",
            new
            {
                model_name = "qwen2.5:3b-instruct",
                purpose = "routing",
                prompt_tokens = 120,
                completion_tokens = 8,
                latency_ms = 300,
                valid_json = false,
                output = "not json at all",
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Created);

        var recorded = await response.Content.ReadFromJsonAsync<PredictionResponse>(
            SentinelApiFactory.Json);

        recorded!.ValidJson.Should().BeFalse();
    }

    [Fact]
    public async Task Recorded_predictions_are_readable_through_the_authenticated_api()
    {
        var internalClient = _factory.CreateInternalClient();
        await internalClient.PostAsJsonAsync(
            "/internal/model-predictions", Prediction(), SentinelApiFactory.Json);

        var client = await _factory.CreateAdminClientAsync();
        var predictions = await client.GetFromJsonAsync<List<PredictionResponse>>(
            "/api/model-predictions?limit=10", SentinelApiFactory.Json);

        predictions.Should().NotBeEmpty();
    }

    [Fact]
    public async Task Reading_predictions_requires_authentication()
    {
        var response = await _factory.CreateAnonymousClient().GetAsync("/api/model-predictions");

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_prediction_with_negative_tokens_is_refused()
    {
        var client = _factory.CreateInternalClient();

        var response = await client.PostAsJsonAsync(
            "/internal/model-predictions",
            new
            {
                model_name = "qwen2.5:3b-instruct",
                purpose = "reasoning",
                prompt_tokens = -1,
                completion_tokens = 5,
                latency_ms = 10,
                valid_json = true,
            },
            SentinelApiFactory.Json);

        response.StatusCode.Should().BeOneOf(
            HttpStatusCode.BadRequest, HttpStatusCode.UnprocessableEntity);
    }

    private static object Prediction(int latencyMs = 500) => new
    {
        model_name = "qwen2.5:3b-instruct",
        purpose = "reasoning",
        prompt_tokens = 400,
        completion_tokens = 60,
        latency_ms = latencyMs,
        valid_json = true,
        output = "{\"summary\":\"ok\"}",
    };

    private sealed record PredictionResponse(
        Guid Id,
        Guid? InvestigationId,
        string ModelName,
        string Purpose,
        int PromptTokens,
        int CompletionTokens,
        int LatencyMs,
        bool ValidJson);
}
