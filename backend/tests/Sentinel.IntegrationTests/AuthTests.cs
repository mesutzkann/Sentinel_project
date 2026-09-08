using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using FluentAssertions;
using Xunit;

namespace Sentinel.IntegrationTests;

/// <summary>
/// Logging in, and what a token does and does not open.
/// </summary>
[Collection(ApiCollection.Name)]
public sealed class AuthTests
{
    private readonly SentinelApiFactory _factory;

    public AuthTests(SentinelApiFactory factory) => _factory = factory;

    [Fact]
    public async Task The_seeded_admin_can_log_in()
    {
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/api/auth/login",
            new { username = SentinelApiFactory.AdminUsername, password = SentinelApiFactory.AdminPassword },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.OK);

        var login = await response.Content.ReadFromJsonAsync<SentinelApiFactory.LoginResponse>(
            SentinelApiFactory.Json);

        login!.AccessToken.Should().NotBeNullOrWhiteSpace();
        login.Username.Should().Be(SentinelApiFactory.AdminUsername);
    }

    [Fact]
    public async Task A_wrong_password_is_rejected()
    {
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/api/auth/login",
            new { username = SentinelApiFactory.AdminUsername, password = "not-the-password" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task An_unknown_user_is_rejected_the_same_way_as_a_wrong_password()
    {
        // Same status either way, so the response does not say which usernames exist.
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/api/auth/login",
            new { username = "nobody", password = "whatever" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task There_is_no_signup_endpoint()
    {
        // By design: accounts are created by an admin, and the first admin is seeded. A signup
        // route appearing later would be a real change, and this is what would notice it.
        var client = _factory.CreateAnonymousClient();

        var response = await client.PostAsJsonAsync(
            "/api/auth/register",
            new { username = "intruder", password = "Intruder123!" },
            SentinelApiFactory.Json);

        response.StatusCode.Should().Be(HttpStatusCode.NotFound);
    }

    [Fact]
    public async Task Me_returns_the_logged_in_user()
    {
        var client = await _factory.CreateAdminClientAsync();

        var me = await client.GetFromJsonAsync<MeResponse>("/api/auth/me", SentinelApiFactory.Json);

        me!.Username.Should().Be(SentinelApiFactory.AdminUsername);
        me.Role.Should().Be("admin");
    }

    [Fact]
    public async Task A_protected_endpoint_refuses_an_anonymous_request()
    {
        var response = await _factory.CreateAnonymousClient().GetAsync("/api/services");

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_malformed_token_is_refused()
    {
        var client = _factory.CreateAnonymousClient();
        client.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", "not.a.jwt");

        var response = await client.GetAsync("/api/services");

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    [Fact]
    public async Task A_token_signed_with_another_key_is_refused()
    {
        // The signature is the whole guarantee. A token with the right shape and claims but a
        // different signing key has to fail, or the key is decorative.
        var client = _factory.CreateAnonymousClient();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                + ".eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJBZG1pbiJ9"
                + ".c2lnbmVkLXdpdGgtc29tZXRoaW5nLWVsc2U");

        var response = await client.GetAsync("/api/services");

        response.StatusCode.Should().Be(HttpStatusCode.Unauthorized);
    }

    private sealed record MeResponse(Guid Id, string Username, string Role);
}
