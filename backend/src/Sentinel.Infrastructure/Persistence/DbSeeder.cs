using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence;

/// <summary>
/// Puts the minimum into an empty database for the platform to be usable: an admin account and
/// the five sample services.
/// </summary>
/// <remarks>
/// Idempotent — it inserts only what is missing, so it can run on every start. There is no signup
/// endpoint by design, which makes the seeded admin the only way in on a fresh database.
/// </remarks>
public sealed class DbSeeder
{
    private readonly SentinelDbContext _db;
    private readonly IPasswordHasher _hasher;
    private readonly IConfiguration _configuration;
    private readonly ILogger<DbSeeder> _logger;

    public DbSeeder(
        SentinelDbContext db,
        IPasswordHasher hasher,
        IConfiguration configuration,
        ILogger<DbSeeder> logger)
    {
        _db = db;
        _hasher = hasher;
        _configuration = configuration;
        _logger = logger;
    }

    public async Task SeedAsync(CancellationToken cancellationToken = default)
    {
        await SeedAdminAsync(cancellationToken);
        await SeedServicesAsync(cancellationToken);
    }

    private async Task SeedAdminAsync(CancellationToken cancellationToken)
    {
        var username = _configuration["Seed:AdminUsername"] ?? "admin";

        if (await _db.Users.AnyAsync(u => u.Username == username, cancellationToken))
        {
            return;
        }

        var password = _configuration["Seed:AdminPassword"]
            ?? throw new InvalidOperationException(
                "Seed:AdminPassword must be set to create the initial admin. See .env.example.");

        _db.Users.Add(new User
        {
            Username = username,
            PasswordHash = _hasher.Hash(password),
            Role = UserRole.Admin,
        });

        await _db.SaveChangesAsync(cancellationToken);
        _logger.LogInformation("Seeded admin user {Username}", username);
    }

    /// <summary>
    /// The five sample services. Names match the OTel <c>service.name</c> the services report,
    /// which is the key the agent joins logs, metrics and traces on — they are not cosmetic.
    /// </summary>
    private async Task SeedServicesAsync(CancellationToken cancellationToken)
    {
        var seeds = new (string Name, string DisplayName, string RepoPath, string HealthUrl)[]
        {
            ("gateway", "API Gateway", "sample-services/gateway", "http://localhost:8080/health"),
            ("users", "Users Service", "sample-services/users", "http://localhost:8081/health"),
            ("orders", "Orders Service", "sample-services/orders", "http://localhost:8082/health"),
            ("payments", "Payments Service", "sample-services/payments", "http://localhost:8083/health"),
            ("notifications", "Notifications Service", "sample-services/notifications", "http://localhost:8084/health"),
        };

        var existing = await _db.Services
            .Select(s => s.Name)
            .ToListAsync(cancellationToken);

        var missing = seeds
            .Where(s => !existing.Contains(s.Name))
            .Select(s => new Service
            {
                Name = s.Name,
                DisplayName = s.DisplayName,
                RepoPath = s.RepoPath,
                HealthUrl = s.HealthUrl,
                MetricsJob = s.Name,
            })
            .ToArray();

        if (missing.Length == 0)
        {
            return;
        }

        _db.Services.AddRange(missing);
        await _db.SaveChangesAsync(cancellationToken);

        _logger.LogInformation("Seeded {Count} services", missing.Length);
    }
}
