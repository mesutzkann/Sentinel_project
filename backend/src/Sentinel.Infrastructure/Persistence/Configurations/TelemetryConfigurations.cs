using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata.Builders;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence.Configurations;

public sealed class ToolCallConfiguration : IEntityTypeConfiguration<ToolCall>
{
    public void Configure(EntityTypeBuilder<ToolCall> builder)
    {
        builder.ToTable("tool_calls");
        builder.HasKey(t => t.Id);

        builder.Property(t => t.Server).HasMaxLength(50).IsRequired();
        builder.Property(t => t.Tool).HasMaxLength(100).IsRequired();
        builder.Property(t => t.ResultSummary).HasMaxLength(4000);
        builder.Property(t => t.Args).HasColumnType("jsonb");

        builder.HasIndex(t => t.InvestigationId);

        // Agent evaluation asks "which tools did the agent use for this scenario", and the
        // remediation audit asks "show me every destructive call".
        builder.HasIndex(t => new { t.Tool, t.CalledAt });
        // Quoted because the column keeps EF's PascalCase default while the table is
        // renamed to snake_case, so an unquoted identifier folds to "is_destructive".
        builder.HasIndex(t => t.IsDestructive).HasFilter("\"IsDestructive\" = true");

        builder.HasOne(t => t.Investigation)
            .WithMany(i => i.ToolCallRecords)
            .HasForeignKey(t => t.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);

        builder.HasOne(t => t.Step)
            .WithMany()
            .HasForeignKey(t => t.StepId)
            .OnDelete(DeleteBehavior.SetNull);

        builder.HasOne(t => t.Approval)
            .WithMany()
            .HasForeignKey(t => t.ApprovalId)
            .OnDelete(DeleteBehavior.SetNull);
    }
}

public sealed class ModelPredictionConfiguration : IEntityTypeConfiguration<ModelPrediction>
{
    public void Configure(EntityTypeBuilder<ModelPrediction> builder)
    {
        builder.ToTable("model_predictions");
        builder.HasKey(m => m.Id);

        builder.Property(m => m.ModelName).HasMaxLength(100).IsRequired();
        builder.Property(m => m.Purpose).HasConversion<string>().HasMaxLength(20);
        builder.Property(m => m.Output).HasColumnType("jsonb");

        builder.HasIndex(m => m.InvestigationId);

        // The base-versus-fine-tuned comparison groups by model and purpose over a time range.
        builder.HasIndex(m => new { m.ModelName, m.Purpose, m.CalledAt });

        builder.HasOne(m => m.Investigation)
            .WithMany()
            .HasForeignKey(m => m.InvestigationId)
            .OnDelete(DeleteBehavior.SetNull);
    }
}

public sealed class EvaluationRunConfiguration : IEntityTypeConfiguration<EvaluationRun>
{
    public void Configure(EntityTypeBuilder<EvaluationRun> builder)
    {
        builder.ToTable("evaluation_runs");
        builder.HasKey(r => r.Id);

        builder.Property(r => r.Kind).HasConversion<string>().HasMaxLength(20);
        builder.Property(r => r.ModelOrConfig).HasMaxLength(200).IsRequired();
        builder.Property(r => r.Notes).HasMaxLength(2000);
        builder.Property(r => r.Metrics).HasColumnType("jsonb");

        builder.HasIndex(r => new { r.Kind, r.StartedAt });
    }
}

public sealed class EvaluationResultConfiguration : IEntityTypeConfiguration<EvaluationResult>
{
    public void Configure(EntityTypeBuilder<EvaluationResult> builder)
    {
        builder.ToTable("evaluation_results");
        builder.HasKey(r => r.Id);

        builder.Property(r => r.CaseId).HasMaxLength(100).IsRequired();
        builder.Property(r => r.Expected).HasColumnType("jsonb");
        builder.Property(r => r.Actual).HasColumnType("jsonb");
        builder.Property(r => r.Details).HasColumnType("jsonb");

        // Reviewing a run means listing its failures first.
        builder.HasIndex(r => new { r.RunId, r.Passed });

        builder.HasOne(r => r.Run)
            .WithMany(run => run.Results)
            .HasForeignKey(r => r.RunId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}
