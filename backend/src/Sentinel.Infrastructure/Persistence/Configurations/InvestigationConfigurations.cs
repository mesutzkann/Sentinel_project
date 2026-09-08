using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata.Builders;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence.Configurations;

public sealed class InvestigationConfiguration : IEntityTypeConfiguration<Investigation>
{
    public void Configure(EntityTypeBuilder<Investigation> builder)
    {
        builder.ToTable("investigations");
        builder.HasKey(i => i.Id);

        builder.Property(i => i.Query).HasMaxLength(2000).IsRequired();
        builder.Property(i => i.RouterIntent).HasMaxLength(64);
        builder.Property(i => i.Status).HasConversion<string>().HasMaxLength(20);
        builder.Property(i => i.FailureReason).HasMaxLength(1000);

        // jsonb rather than text: it is queryable, and router evaluation reads these rows
        // directly to replay past decisions.
        builder.Property(i => i.RouterOutput).HasColumnType("jsonb");

        builder.HasIndex(i => i.IncidentId);
        builder.HasIndex(i => i.Status);

        builder.HasOne(i => i.Incident)
            .WithMany(inc => inc.Investigations)
            .HasForeignKey(i => i.IncidentId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}

public sealed class InvestigationStepConfiguration : IEntityTypeConfiguration<InvestigationStep>
{
    public void Configure(EntityTypeBuilder<InvestigationStep> builder)
    {
        builder.ToTable("investigation_steps");
        builder.HasKey(s => s.Id);

        builder.Property(s => s.State).HasMaxLength(64).IsRequired();
        builder.Property(s => s.Message).HasMaxLength(2000).IsRequired();
        builder.Property(s => s.Payload).HasColumnType("jsonb");

        // Unique, not merely indexed: the sequence number is how the backend detects a callback
        // that arrived twice (ADR-0002 delivery is at-least-once, not exactly-once).
        builder.HasIndex(s => new { s.InvestigationId, s.Sequence }).IsUnique();

        builder.HasOne(s => s.Investigation)
            .WithMany(i => i.Steps)
            .HasForeignKey(s => s.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}

public sealed class EvidenceConfiguration : IEntityTypeConfiguration<Evidence>
{
    public void Configure(EntityTypeBuilder<Evidence> builder)
    {
        builder.ToTable("evidence");
        builder.HasKey(e => e.Id);

        builder.Property(e => e.Source).HasConversion<string>().HasMaxLength(30);
        builder.Property(e => e.Summary).HasMaxLength(2000).IsRequired();
        builder.Property(e => e.Raw).HasColumnType("jsonb");
        builder.Property(e => e.Weight).HasPrecision(3, 2);

        builder.HasIndex(e => e.InvestigationId);

        builder.HasOne(e => e.Investigation)
            .WithMany(i => i.Evidence)
            .HasForeignKey(e => e.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);

        builder.HasOne(e => e.Step)
            .WithMany()
            .HasForeignKey(e => e.StepId)
            .OnDelete(DeleteBehavior.SetNull);
    }
}

public sealed class HypothesisConfiguration : IEntityTypeConfiguration<Hypothesis>
{
    public void Configure(EntityTypeBuilder<Hypothesis> builder)
    {
        builder.ToTable("hypotheses");
        builder.HasKey(h => h.Id);

        builder.Property(h => h.Title).HasMaxLength(300).IsRequired();
        builder.Property(h => h.Description).HasMaxLength(4000);
        builder.Property(h => h.Score).HasPrecision(3, 2);

        builder.HasIndex(h => new { h.InvestigationId, h.Rank });

        builder.HasOne(h => h.Investigation)
            .WithMany(i => i.Hypotheses)
            .HasForeignKey(h => h.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}

public sealed class RootCauseConfiguration : IEntityTypeConfiguration<RootCause>
{
    public void Configure(EntityTypeBuilder<RootCause> builder)
    {
        builder.ToTable("root_causes");
        builder.HasKey(r => r.Id);

        builder.Property(r => r.Title).HasMaxLength(300).IsRequired();
        builder.Property(r => r.Category).HasMaxLength(64).IsRequired();
        builder.Property(r => r.Confidence).HasPrecision(3, 2);
        builder.Property(r => r.ValidatorConfidence).HasPrecision(3, 2);
        builder.Property(r => r.Explanation).HasMaxLength(8000);
        builder.Property(r => r.ValidatorOutput).HasColumnType("jsonb");

        builder.HasIndex(r => r.InvestigationId);

        // Agent evaluation groups by category to report per-scenario accuracy.
        builder.HasIndex(r => r.Category);

        builder.HasOne(r => r.Investigation)
            .WithMany(i => i.RootCauses)
            .HasForeignKey(r => r.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);

        builder.HasOne(r => r.Hypothesis)
            .WithMany()
            .HasForeignKey(r => r.HypothesisId)
            .OnDelete(DeleteBehavior.SetNull);
    }
}
