using Microsoft.EntityFrameworkCore;
using Sentinel.Application.Common;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence;

/// <summary>
/// The backend's database. Owns the <c>sentinel</c> schema and nothing else — the AI service owns
/// <c>rag</c> and each sample service owns its own, with no cross-schema writes in either
/// direction.
/// </summary>
public sealed class SentinelDbContext : DbContext, ISentinelDbContext
{
    public const string Schema = "sentinel";

    public SentinelDbContext(DbContextOptions<SentinelDbContext> options) : base(options)
    {
    }

    public DbSet<Service> Services => Set<Service>();

    public DbSet<User> Users => Set<User>();

    public DbSet<Incident> Incidents => Set<Incident>();

    public DbSet<Investigation> Investigations => Set<Investigation>();

    public DbSet<InvestigationStep> InvestigationSteps => Set<InvestigationStep>();

    public DbSet<Evidence> Evidence => Set<Evidence>();

    public DbSet<Hypothesis> Hypotheses => Set<Hypothesis>();

    public DbSet<RootCause> RootCauses => Set<RootCause>();

    public DbSet<Recommendation> Recommendations => Set<Recommendation>();

    public DbSet<Postmortem> Postmortems => Set<Postmortem>();

    public DbSet<ToolCall> ToolCalls => Set<ToolCall>();

    public DbSet<ModelPrediction> ModelPredictions => Set<ModelPrediction>();

    public DbSet<EvaluationRun> EvaluationRuns => Set<EvaluationRun>();

    public DbSet<EvaluationResult> EvaluationResults => Set<EvaluationResult>();

    /// <inheritdoc />
    public Task ReloadAsync<TEntity>(TEntity entity, CancellationToken cancellationToken = default)
        where TEntity : class =>
        Entry(entity).ReloadAsync(cancellationToken);

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.HasDefaultSchema(Schema);
        modelBuilder.ApplyConfigurationsFromAssembly(typeof(SentinelDbContext).Assembly);
    }
}
