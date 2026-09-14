using System.Text;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Microsoft.IdentityModel.Tokens;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Infrastructure.Ai;
using Sentinel.Infrastructure.Auth;
using Sentinel.Infrastructure.Persistence;

namespace Sentinel.Infrastructure;

public static class DependencyInjection
{
    public static IServiceCollection AddInfrastructure(
        this IServiceCollection services,
        IConfiguration configuration)
    {
        var connectionString = configuration.GetConnectionString("Postgres")
            ?? throw new InvalidOperationException(
                "ConnectionStrings:Postgres is not configured. See .env.example.");

        services.AddDbContext<SentinelDbContext>(options =>
            options.UseNpgsql(connectionString, npgsql =>
                npgsql.MigrationsHistoryTable("__ef_migrations_history", SentinelDbContext.Schema)));

        services.AddScoped<ISentinelDbContext>(sp => sp.GetRequiredService<SentinelDbContext>());

        services.AddHttpContextAccessor();
        services.AddScoped<ICurrentUser, CurrentUser>();
        services.AddSingleton<IPasswordHasher, BCryptPasswordHasher>();

        services.AddOptions<JwtOptions>()
            .Bind(configuration.GetSection(JwtOptions.SectionName))
            .ValidateOnStart();

        services.AddSingleton<IJwtTokenService, JwtTokenService>();

        AddJwtAuthentication(services, configuration);

        services.AddScoped<DbSeeder>();

        AddAiService(services, configuration);

        return services;
    }

    /// <summary>The outbound half of ADR-0002: starting a run, and the token its callbacks carry.</summary>
    private static void AddAiService(IServiceCollection services, IConfiguration configuration)
    {
        services.AddOptions<AiServiceOptions>()
            .Bind(configuration.GetSection(AiServiceOptions.SectionName))
            .Configure(options =>
            {
                // The AI service reads AI_SERVICE_URL and BACKEND_BASE_URL out of the
                // repository's .env, and so does this, rather than the same two addresses being
                // configured twice in two formats and drifting apart.
                options.BaseUrl = configuration["AI_SERVICE_URL"] ?? options.BaseUrl;
                options.CallbackBaseUrl = configuration["BACKEND_BASE_URL"] ?? options.CallbackBaseUrl;
            });

        services.AddHttpClient(AiServiceClient.HttpClientName)
            .ConfigureHttpClient((sp, client) =>
            {
                var options = sp.GetRequiredService<IOptions<AiServiceOptions>>().Value;

                client.BaseAddress = new Uri(options.BaseUrl.TrimEnd('/') + "/");
                client.Timeout = TimeSpan.FromSeconds(options.TimeoutSeconds);
            });

        services.AddScoped<IAiServiceClient, AiServiceClient>();
        services.AddSingleton<ICallbackTokenService, CallbackTokenService>();

        // Phase 10. Shares its secret with the AI service and the MCP servers: the three
        // verify the same token, and a value set in one of them and not the others makes
        // every approval fail at whichever layer disagrees.
        services.AddSingleton<IApprovalTokenService, ApprovalTokenService>();
    }

    private static void AddJwtAuthentication(IServiceCollection services, IConfiguration configuration)
    {
        var jwt = configuration.GetSection(JwtOptions.SectionName).Get<JwtOptions>() ?? new JwtOptions();

        services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
            .AddJwtBearer(options =>
            {
                options.TokenValidationParameters = new TokenValidationParameters
                {
                    ValidateIssuer = true,
                    ValidIssuer = jwt.Issuer,
                    ValidateAudience = true,
                    ValidAudience = jwt.Audience,
                    ValidateIssuerSigningKey = true,
                    IssuerSigningKey = new SymmetricSecurityKey(
                        Encoding.UTF8.GetBytes(jwt.SigningKey)),
                    ValidateLifetime = true,

                    // Default is five minutes of slack, which is more than a local stack needs and
                    // makes expiry behaviour confusing to demonstrate.
                    ClockSkew = TimeSpan.FromSeconds(30),
                };

                // SignalR cannot set an Authorization header on its WebSocket handshake, so the
                // hub accepts the token as a query parameter instead.
                options.Events = new JwtBearerEvents
                {
                    OnMessageReceived = context =>
                    {
                        var accessToken = context.Request.Query["access_token"];
                        var path = context.HttpContext.Request.Path;

                        if (!string.IsNullOrEmpty(accessToken) && path.StartsWithSegments("/hubs"))
                        {
                            context.Token = accessToken;
                        }

                        return Task.CompletedTask;
                    },
                };
            });

        services.AddAuthorizationBuilder()
            .AddPolicy(Policies.RequireEngineer, policy =>
                policy.RequireRole(nameof(UserRole.Engineer), nameof(UserRole.Admin)))
            .AddPolicy(Policies.RequireAdmin, policy =>
                policy.RequireRole(nameof(UserRole.Admin)));
    }
}

/// <summary>Authorisation policy names, so controllers and registration cannot drift apart.</summary>
public static class Policies
{
    /// <summary>Create incidents, start investigations, approve remediations.</summary>
    public const string RequireEngineer = "RequireEngineer";

    /// <summary>Manage services and users.</summary>
    public const string RequireAdmin = "RequireAdmin";
}
