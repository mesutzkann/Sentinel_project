using System.Text;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.IdentityModel.Tokens;
using Sentinel.Application.Common;
using Sentinel.Domain;
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

        return services;
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
